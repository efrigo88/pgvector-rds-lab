import os
import json
from datetime import datetime
from typing import List, Dict, Any, BinaryIO

import boto3
from botocore.exceptions import ClientError
import pyspark.sql.functions as F
from sqlalchemy import create_engine, text, Engine
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_postgres import PGVector
from pyspark.sql import SparkSession, DataFrame
import pyspark.sql.types as T
from docling.datamodel.document import InputDocument
from docling.document_converter import DocumentConverter
from langchain_ollama import OllamaEmbeddings


# Initialize S3 client
s3_client = boto3.client("s3")

SCHEMA = T.StructType(
    [
        T.StructField("id", T.StringType(), True),
        T.StructField("chunk", T.StringType(), True),
        T.StructField(
            "metadata",
            T.StructType(
                [
                    T.StructField("source", T.StringType(), True),
                    T.StructField("chunk_index", T.IntegerType(), True),
                    T.StructField("title", T.StringType(), True),
                    T.StructField("chunk_size", T.IntegerType(), True),
                ]
            ),
            True,
        ),
        T.StructField("processed_at", T.TimestampType(), True),
        T.StructField("processed_dt", T.StringType(), True),
        T.StructField("embeddings", T.ArrayType(T.FloatType()), True),
    ]
)

# Create Spark session
spark = (
    SparkSession.builder.appName("TestSpark")
    .master(os.getenv("SPARK_THREADS"))
    .config("spark.driver.memory", os.getenv("SPARK_DRIVER_MEMORY"))
    .config(
        "spark.sql.shuffle.partitions",
        os.getenv("SPARK_SHUFFLE_PARTITIONS"),
    )
    .config(
        "spark.sql.extensions",
        "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    )
    .config(
        "spark.sql.catalog.spark_catalog",
        "org.apache.iceberg.spark.SparkSessionCatalog",
    )
    .config("spark.sql.catalog.spark_catalog.type", "hadoop")
    .config(
        "spark.sql.catalog.spark_catalog.warehouse",
        f"s3a://{os.getenv('S3_BUCKET')}/warehouse/default",
    )
    .config(
        "spark.jars.packages",
        "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.5.0,"
        "org.apache.hadoop:hadoop-aws:3.3.4,"
        "com.amazonaws:aws-java-sdk-bundle:1.12.262",
    )
    .config(
        "spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem"
    )
    .config(
        "spark.hadoop.fs.s3a.aws.credentials.provider",
        "com.amazonaws.auth.DefaultAWSCredentialsProviderChain",
    )
    .config("spark.hadoop.fs.s3a.endpoint", "s3.amazonaws.com")
    .config("spark.hadoop.fs.s3a.path.style.access", "false")
    .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "true")
    .getOrCreate()
)


class S3Error(Exception):
    """Custom exception for S3-related errors."""


def create_dataframe(
    ids: List[str],
    chunks: List[str],
    metadatas: List[Dict[str, Any]],
    embeddings: List[List[float]],
) -> DataFrame:
    """Create and save DataFrame with processed data."""
    df = spark.createDataFrame(
        [
            {
                "id": id_val,
                "chunk": chunk,
                "metadata": metadata,
                "processed_at": datetime.now(),
                "processed_dt": datetime.now().strftime("%Y-%m-%d"),
                "embeddings": embedding,
            }
            for id_val, chunk, metadata, embedding in zip(
                ids, chunks, metadatas, embeddings
            )
        ],
        schema=SCHEMA,
    )
    return df


def deduplicate_data(df: DataFrame) -> DataFrame:
    """Deduplicate data and return processed DataFrame."""
    df = (
        df.orderBy(F.col("processed_at").desc())
        .groupBy("id")
        .agg(
            F.first("processed_at").alias("processed_at"),
            F.first("processed_dt").alias("processed_dt"),
            F.first("chunk").alias("chunk"),
            F.first("metadata").alias("metadata"),
            F.first("embeddings").alias("embeddings"),
        )
    )
    return df


def get_s3_bucket_and_key(s3_path: str) -> tuple[str, str]:
    """Extract bucket name and key from S3 path."""
    if not (s3_path.startswith("s3://") or s3_path.startswith("s3a://")):
        raise ValueError(
            "Path must be an S3 path starting with 's3://' or 's3a://'"
        )
    path_without_prefix = (
        s3_path[5:] if s3_path.startswith("s3://") else s3_path[6:]
    )
    bucket_name = path_without_prefix.split("/")[0]
    key = "/".join(path_without_prefix.split("/")[1:])
    return bucket_name, key


def read_from_s3(s3_path: str) -> BinaryIO:
    """Read file from S3."""
    bucket_name, key = get_s3_bucket_and_key(s3_path)
    try:
        response = s3_client.get_object(Bucket=bucket_name, Key=key)
        return response["Body"]
    except ClientError as e:
        raise S3Error(f"Error reading from S3: {str(e)}") from e


def write_to_s3(file_path: str, s3_path: str) -> None:
    """Write file to S3."""
    bucket_name, key = get_s3_bucket_and_key(s3_path)
    try:
        s3_client.upload_file(file_path, bucket_name, key)
    except ClientError as e:
        raise S3Error(f"Error writing to S3: {str(e)}") from e


def parse_pdf(source_path: str) -> InputDocument:
    """Parse the PDF document using DocumentConverter."""
    converter = DocumentConverter()
    if source_path.startswith("s3://"):
        # Read from S3 and save to temporary file
        s3_file = read_from_s3(source_path)
        temp_file = f"/tmp/{source_path.split('/')[-1]}"
        with open(temp_file, "wb") as f:
            f.write(s3_file.read())
        try:
            result = converter.convert(temp_file)
        finally:
            # Clean up temporary file
            if os.path.exists(temp_file):
                os.remove(temp_file)
    else:
        # Read from local file
        result = converter.convert(source_path)
    return result.document


def get_text_content(doc: InputDocument) -> List[str]:
    """Extract text content from the document."""
    return [
        text_item.text.strip()
        for text_item in doc.texts
        if text_item.text.strip() and text_item.label == "text"
    ]


def get_chunks(
    text_content: List[str], chunk_size: int = 750, chunk_overlap: int = 100
) -> List[str]:
    """Split text content into semantically meaningful chunks."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ".", "!", "?", " ", ""],
    )
    chunks = []
    for content in text_content:
        chunks.extend(splitter.split_text(content))
    if not chunks:
        raise ValueError("No text chunks found in the document.")
    return chunks


def get_ids(chunks: List[str], source_path: str) -> List[str]:
    """Generate unique IDs for each chunk."""
    filename = source_path.split("/")[-1]
    return [f"{filename}_chunk_{i}" for i in range(len(chunks))]


def get_metadata(
    chunks: List[str],
    doc: InputDocument,
    source_path: str,
) -> List[Dict[str, Any]]:
    """Generate metadata for each chunk."""
    filename = source_path.split("/")[-1]
    return [
        {
            "source": filename,
            "chunk_index": i,
            "title": doc.name,
            "chunk_size": len(chunk),
        }
        for i, chunk in enumerate(chunks)
    ]


def get_embeddings(
    chunks: List[str],
    model: OllamaEmbeddings,
) -> List[List[float]]:
    """Get embeddings for a list of chunks using Ollama embeddings."""
    return model.embed_documents(chunks)


def create_iceberg_table(df: DataFrame, table_name: str) -> None:
    """Create Iceberg table if it doesn't exist."""
    # Register DataFrame as a temporary view
    df.createOrReplaceTempView("temp_df")

    # Create table with S3 location
    spark.sql(
        f"CREATE TABLE IF NOT EXISTS {table_name} "
        f"USING iceberg "
        f"LOCATION 's3a://{os.getenv('S3_BUCKET')}/warehouse/default/{table_name}' "
        "AS SELECT * FROM temp_df LIMIT 0"
    )

    # Save DataFrame to Iceberg table
    df.write.format("iceberg").mode("overwrite").saveAsTable(table_name)
    print(f"✅ Saved Iceberg table {table_name} to S3")


def save_json_data(
    data: List[Dict[str, Any]], file_path: str, overwrite: bool = True
) -> None:
    """Save data to a JSONL file (one JSON object per line)."""
    if not overwrite and os.path.exists(file_path):
        raise FileExistsError(
            f"File {file_path} already exists and overwrite=False"
        )

    # Create a temporary file
    temp_file = f"/tmp/{os.path.basename(file_path)}"

    # Write to temporary file
    with open(temp_file, "w", encoding="utf-8") as f:
        for item in data:
            json.dump(item, f, ensure_ascii=False)
            f.write("\n")

    # If it's an S3 path, upload the file
    if file_path.startswith(("s3://", "s3a://")):
        write_to_s3(temp_file, file_path)
        # Clean up temporary file
        os.remove(temp_file)
    else:
        # Move the temporary file to the final location
        os.rename(temp_file, file_path)


def get_db_connection_string() -> str:
    """Create a connection string for PostgreSQL."""
    return (
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER')}:"
        f"{os.getenv('POSTGRES_PASSWORD')}@{os.getenv('POSTGRES_HOST')}:"
        f"{os.getenv('POSTGRES_PORT')}/{os.getenv('POSTGRES_DB')}"
    )


def ensure_pgvector_extension_exists() -> None:
    """Ensure the pgvector extension is created in the database."""
    engine: Engine = create_engine(get_db_connection_string())
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))


def init_vector_store(embeddings: OllamaEmbeddings) -> PGVector:
    """Initialize the vector store with LangChain's PGVector."""
    connection_string = get_db_connection_string()
    collection_name = "documents"

    # Ensure pgvector extension exists before initializing PGVector
    ensure_pgvector_extension_exists()

    return PGVector(
        embeddings=embeddings,
        collection_name=collection_name,
        connection=connection_string,
        use_jsonb=True,
    )


def store_in_postgres(df: DataFrame, embeddings: OllamaEmbeddings) -> None:
    """Store data in PostgreSQL using LangChain's PGVector."""
    vector_store: PGVector = init_vector_store(embeddings)

    # Convert DataFrame rows to LangChain Documents
    documents = []
    ids = []
    for row in df.collect():
        doc = Document(
            page_content=row.chunk,
            metadata={
                "id": row.id,
                "source": row.metadata["source"],
                "chunk_index": row.metadata["chunk_index"],
                "title": row.metadata["title"],
                "chunk_size": row.metadata["chunk_size"],
                "processed_at": row.processed_at.isoformat(),
                "processed_dt": row.processed_dt,
            },
        )
        documents.append(doc)
        ids.append(row.id)

    # Add documents to vector store with upsert
    vector_store.add_documents(documents, ids=ids)
    print(f"📊 Total documents in PostgreSQL: {len(documents)}")


def prepare_queries(
    queries: List[str],
    embeddings: OllamaEmbeddings,
) -> List[Dict[str, Any]]:
    """Run queries and prepare results in json format using LangChain's PGVector."""
    vector_store: PGVector = init_vector_store(embeddings)
    all_results = []

    for query in queries:
        # Perform similarity search using LangChain's PGVector
        results = vector_store.similarity_search(
            query,
            k=3,
        )

        query_result = {
            "processed_at": datetime.now().isoformat(),
            "query": query,
            "results": [
                {
                    "text": doc.page_content,
                    "metadata": doc.metadata,
                    "similarity": 1.0,  # LangChain doesn't provide similarity scores
                }
                for doc in results
            ],
        }
        all_results.append(query_result)

    return all_results

#!/bin/bash

# Exit on error
set -e

# Source environment variables
source .env

# Export AWS credentials from .env
export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY
export AWS_DEFAULT_REGION

echo "🔑 Initializing and applying Terraform..."
cd infra
terraform init
terraform apply -auto-approve

# Configuration
AWS_REGION="eu-west-1"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "Using AWS Region: ${AWS_REGION}"
echo "Using AWS Account ID: ${AWS_ACCOUNT_ID}"

cd ..
ECR_REPOSITORY="poc-pipeline"
IMAGE_TAG="latest"

# Ask for confirmation
echo "Note: Select 'y' if this is your first time running or if you've modified the Docker image"
read -p "Do you want to build and push a new Docker image? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]
then
    # Build Docker image
    echo "📦 Building Docker image..."
    docker buildx build --platform linux/amd64 -t ${ECR_REPOSITORY}:${IMAGE_TAG} .

    # Login to ECR
    echo "🔑 Logging in to ECR..."
    aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

    # Tag and push Docker image
    echo "🏷️  Tagging and pushing Docker image..."
    docker tag ${ECR_REPOSITORY}:${IMAGE_TAG} ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}:${IMAGE_TAG}
    docker push ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}:${IMAGE_TAG}
else
    echo "Skipping Docker image build and push..."
fi

echo "✅ Deployment completed successfully!"
echo "
📋 To run the ECS task, use:
sh scripts/run_ecs_task.sh
"

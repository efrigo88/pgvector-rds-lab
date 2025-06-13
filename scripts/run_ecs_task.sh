#!/bin/bash

# Exit on error
set -e

# Source environment variables
source .env

# Export AWS credentials from .env
export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY
export AWS_DEFAULT_REGION

# Configuration
AWS_REGION="eu-west-1"

echo "🚀 Starting ECS task..."

# Get Terraform outputs
cd infra
SUBNET_ID=$(terraform output -json subnet_ids | jq -r '.[0]')
SECURITY_GROUP_ID=$(terraform output -raw security_group_id)
TASK_DEFINITION_ARN=$(terraform output -raw task_definition_arn)
cd ..

# Run the ECS task
echo "📋 Running ECS task..."
aws ecs run-task \
  --region ${AWS_REGION} \
  --cluster poc-pipeline-cluster \
  --task-definition ${TASK_DEFINITION_ARN} \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNET_ID}],securityGroups=[${SECURITY_GROUP_ID}],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"poc-container\",\"environment\":[
    {\"name\":\"THREADS\",\"value\":\"${THREADS}\"},
    {\"name\":\"DRIVER_MEMORY\",\"value\":\"${DRIVER_MEMORY}\"},
    {\"name\":\"SHUFFLE_PARTITIONS\",\"value\":\"${SHUFFLE_PARTITIONS}\"},
    {\"name\":\"OLLAMA_HOST\",\"value\":\"http://localhost:11434\"}
  ]}]}" \
  --no-cli-pager > /dev/null

echo "Go to AWS ECS console to monitor the task status."

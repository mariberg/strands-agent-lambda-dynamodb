# strands-agent-lambda-dynamodb

Minimal demo: AWS Lambda + DynamoDB single-table + Strands agent (Bedrock).  
No Cognito, no API Gateway — invoke directly via Lambda Function URL.

## Architecture

- **Lambda Function URL** — curl-friendly, no auth
- **Single DynamoDB table** (`GardenApp-<env>`) — generic `pk`/`sk` key scheme
- **Strands agent** — invoked async via Lambda self-invoke; writes back to the same table

## What's stripped vs. a production setup

| Production | This demo |
|---|---|
| Cognito JWT auth | Hardcoded `DEMO_GARDEN_ID` / `DEMO_USER_ID` env vars |
| API Gateway REST API | Lambda Function URL (`AuthType: NONE`) |
| Multiple DynamoDB tables | Single table, prefixed keys |

## Prerequisites

- AWS account with an IAM user or role that can deploy CloudFormation, Lambda, DynamoDB, and IAM resources
- Amazon Bedrock **Nova Pro** (`amazon.nova-pro-v1:0`) model access enabled in your Bedrock region. Check and enable model access in the [Bedrock console](https://console.aws.amazon.com/bedrock/home#/modelaccess). Note that quotas are per-region — make sure you have quota in the region you set as `BedrockRegion` (defaults to `us-east-1`).

## Deploy

### 0. Set your configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
# Edit .env with your bucket name, region, etc.
```

Then load the variables into your shell:

```bash
source .env
```

> **Note:** `source .env` must be run in each new terminal session before using the commands below — the variables don't persist automatically. Re-run it any time you open a new tab or window.
>
> `.env` is git-ignored so your settings won't be committed.

### 1. Package the code

```bash
pip3 install -r requirements.txt -t package/ \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --implementation cp \
  --only-binary=:all:
cp -r src package/
cd package && zip -r ../function.zip . && cd ..
```

### 2. Create an S3 bucket

Skip this step if you already have a bucket you want to use.

```bash
aws s3 mb s3://$S3_BUCKET --region $AWS_REGION
```

S3 bucket names are globally unique across all AWS accounts, so choose something distinctive (e.g. `strands-demo-yourname`).

### 3. Upload to S3

```bash
aws s3 cp function.zip s3://$S3_BUCKET/strands-demo/function.zip
```

### 4. Deploy the CloudFormation stack

```bash
aws cloudformation deploy \
  --template-file infrastructure/template.yaml \
  --stack-name $STACK_NAME \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $AWS_REGION \
  --parameter-overrides \
      CodeS3Bucket=$S3_BUCKET \
      CodeS3Key=strands-demo/function.zip
```

### 5. Get the Function URL

```bash
aws cloudformation describe-stacks \
  --stack-name $STACK_NAME \
  --region $AWS_REGION \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue" \
  --output text
```

## Seed demo data

```bash
python3 scripts/seed_species.py --env $ENV --region $AWS_REGION
python3 scripts/seed_demo_data.py --env $ENV --region $AWS_REGION
```

## API endpoints

Set `BASE` to the URL from Step 5:

```bash
BASE=https://<your-function-url-id>.lambda-url.$AWS_REGION.on.aws

# List all plants in the garden
curl "$BASE/api/v1/plants"

# Add a new plant
curl -X POST "$BASE/api/v1/plants" \
  -H "Content-Type: application/json" \
  -d '{"species_id": "solanum-lycopersicum", "nickname": "Big Red", "emoji": "🍅", "garden_location": "South bed"}'

# Get a single plant
curl "$BASE/api/v1/plants/<instance_id>"

# List action logs for a plant (paginated)
curl "$BASE/api/v1/plants/<instance_id>/action-logs?limit=10&offset=0"

# Create an action log entry
curl -X POST "$BASE/api/v1/plants/<instance_id>/action-logs" \
  -H "Content-Type: application/json" \
  -d '{"action_type": "watered", "notes": "Watered thoroughly."}'

# Get the AI-generated dashboard summary
curl "$BASE/api/v1/dashboard/summary"

# Trigger an async agent session (refreshes dashboard, can update plant statuses)
curl -X POST "$BASE/api/v1/agent/session-open"

# Chat with the garden agent (synchronous — waits for the reply)
curl -X POST "${BASE}api/v1/agent/chat" \
  -H "Content-Type: application/json" \
  -d '{"message": "Which of my plants need watering today?"}'

# Continue the conversation (history is saved automatically per garden)
curl -X POST "${BASE}api/v1/agent/chat" \
  -H "Content-Type: application/json" \
  -d '{"message": "I just watered the basil"}'

# Reset the conversation and start fresh
curl -X DELETE "${BASE}api/v1/agent/chat"
```

## DynamoDB key scheme

```
PK=GARDEN#<id>   SK=METADATA
PK=GARDEN#<id>   SK=PLANT#<instance_id>
PK=GARDEN#<id>   SK=LOG#<instance_id>#<occurred_at>#<action_id>
PK=GARDEN#<id>   SK=DASHBOARD
PK=SPECIES#<id>  SK=METADATA
PK=USER#<id>     SK=PROFILE
```

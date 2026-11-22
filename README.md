# strands-agent-lambda-dynamodb

Minimal demo of a gardening agent: AWS Lambda + DynamoDB single-table + Strands agents (Amazon Bedrock). The Lambda function is invoked directly through a Lambda Function URL.

> **Platform:** the commands in this README are for macOS and Linux (bash/zsh). On Windows, run them inside [WSL 2](https://learn.microsoft.com/windows/wsl/install).

> ⚠️ **This endpoint is unauthenticated.** The Lambda Function URL has `AuthType: NONE` and is open to the public internet. Anyone with the URL can read and change the demo garden and trigger Bedrock calls. It is intended for short-lived demos only. **Delete the stack when you are done** (see [Teardown](#teardown)).

## Architecture

- **Lambda Function URL** — curl-friendly, no auth. The function can only be invoked through the URL, not directly (see `ApiFunctionInvokePermission` in `infrastructure/template.yaml`).
- **Single DynamoDB table** (`GardenApp-<env>`) — generic `pk`/`sk` key scheme.
- **Two Strands agents**, both in `src/api/agent_runner.py`:
  - **Chat agent** — runs synchronously on `POST /api/v1/agent/chat`. It can answer questions, add plants and log actions.
  - **Session-open agent** — runs asynchronously through a Lambda self-invoke on `POST /api/v1/agent/session-open`. It reads the garden, checks the weather and saves a dashboard summary.
- **Shared service layer** — plants and action logs are written by one module, `src/api/services/garden_service.py`. The REST routes and the agent tools both call it, so validation and item format live in one place. Invalid input raises `ApiError`. REST turns that into a 4xx response, and the agent tools return the same message to the model so it can retry or explain the problem.

## Guardrails

**The model never chooses which user's garden it acts on.** Identity is resolved in code before the agent runs, and no tool accepts a user or garden ID as input.

- **Identity** — in this demo, the garden and user come from the `DemoGardenId` and `DemoUserId` stack parameters in `infrastructure/template.yaml` (defaults `demo-garden-1` and `demo-user-1`). They reach the Lambda as the `DEMO_GARDEN_ID` and `DEMO_USER_ID` environment variables, and `src/api/middleware.py` (`require_garden`) injects them into every request. In production, this is where a verified JWT would be used instead.
- **Binding** — `run_chat` and `run_session_open` in `src/api/agent_runner.py` bind the garden to a per-request context before the agent starts.
- **Tool schemas** — no garden tool takes `garden_id` or `user_id`, so the schema sent to the model has no field for one. Each tool reads the garden from the context and refuses to run without it.
- **Ownership checks** — `garden_service` rejects writes to plants that aren't in the bound garden, which covers invented IDs and other users' plants.
- **Narrow tools only** — the agents have no general-purpose tools. Weather comes from `get_weather`, which takes no input: it calls a fixed Open-Meteo URL with the garden's stored coordinates. The model decides when to ask for weather, never which URL is called or what is sent.
- **System prompt** — tells the model to decline requests about other users. This is a soft layer that shapes behaviour. The code above is what guarantees another user's data can't be touched.

The tests in `tests/` fail if any of these guardrails is removed.

## Prerequisites

- AWS account with an IAM user or role that can deploy CloudFormation, Lambda, DynamoDB and IAM resources
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), configured with credentials for that account
- Python 3.12 or later, plus `zip` and `curl`
- Amazon Bedrock **Nova Pro** (`amazon.nova-pro-v1:0`) model access in the Bedrock region. Check and enable model access in the [Bedrock console](https://console.aws.amazon.com/bedrock/home#/modelaccess). The Bedrock region is the stack's `BedrockRegion` parameter, which defaults to `us-east-1` (see [deploy step 4](#4-deploy-the-cloudformation-stack) to change it). Quotas are per region, so make sure you have Nova Pro quota there.

## Running the tests

The tests run locally with no AWS credentials, no environment variables and no network access. Strands is stubbed out, and every DynamoDB call and the weather API are mocked.

One-time setup, from the project root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

Then run:

```bash
.venv/bin/python -m pytest tests/ -v
```

**Useful options**

```bash
# Run just the guardrail tests
.venv/bin/python -m pytest tests/test_garden_tool_guardrails.py -v

# Stop on the first failure
.venv/bin/python -m pytest tests/ -x

# Run a single test class
.venv/bin/python -m pytest tests/test_garden_tool_guardrails.py::TestToolSchemas -v
```

**What the tests cover**

| File | What it covers |
|---|---|
| `test_garden_tool_guardrails.py` | Guardrails on the deployed agent tools in `src/api/agent_runner.py`: no tool accepts `garden_id`/`user_id`, the runners bind the garden before the agent runs, tools refuse without a bound garden, writes to plants outside the garden are rejected, reads only touch the bound garden, `get_weather` only calls Open-Meteo with the stored coordinates, and injected IDs fail. Also checks that the agent tools and the REST routes both write through `garden_service` and produce identical items. |
| `test_garden_service.py` | The shared write rules in `src/api/services/garden_service.py`: required fields, valid action types, severities and timestamps, plant ownership, and the exact item format written to DynamoDB. Bad input is rejected, never silently dropped. |

The tests check the code, not the model's behaviour. Whether the model follows the system prompt (for example, declining a request about another user) needs a manual check against the deployed Lambda.

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

> **Note:** run `source .env` in every new terminal before using the commands below. The variables don't persist between sessions. If a command fails with `expected one argument`, a variable is empty and you probably need to run `source .env` again.
>
> Every line in `.env` should start with `export` (as in `.env.example`), so the values also reach the AWS CLI and the seed scripts. `.env` is git-ignored, so your settings won't be committed.

### 1. Package the code

```bash
python3 -m pip install -r requirements.txt -t package/ \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --implementation cp \
  --only-binary=:all:
rm -rf package/src function.zip   # start clean so deleted files don't linger in the zip
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
aws s3 cp function.zip s3://$S3_BUCKET/strands-demo/function.zip --region $AWS_REGION
```

### 4. Deploy the CloudFormation stack

```bash
aws cloudformation deploy \
  --template-file infrastructure/template.yaml \
  --stack-name $STACK_NAME \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $AWS_REGION \
  --parameter-overrides \
      Environment=$ENV \
      CodeS3Bucket=$S3_BUCKET \
      CodeS3Key=strands-demo/function.zip
```

Optional extra entries for the `--parameter-overrides` list:

- `BedrockRegion=<region>` calls Bedrock in a different region from the default `us-east-1`.
- `ReservedConcurrency=0` removes the function's reserved concurrency. By default the stack reserves 5 concurrent executions as a cost ceiling on the public URL. Lambda must keep 10 executions unreserved per account, so if your account's total limit is below 15 the deploy fails with `decreases account's UnreservedConcurrentExecution below its minimum value of [10]`. Check your limit with `aws lambda get-account-settings --region $AWS_REGION --query AccountLimit.ConcurrentExecutions`. If it's 10, the account limit already acts as a ceiling, so `0` is safe for this demo.

Re-run this step whenever you change `infrastructure/template.yaml`.

### 5. Get the Function URL

```bash
BASE=$(aws cloudformation describe-stacks \
  --stack-name $STACK_NAME \
  --region $AWS_REGION \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue" \
  --output text)
BASE=${BASE%/}   # the URL ends with "/"; strip it so "$BASE/api/..." has no double slash
echo $BASE
```

Copy the printed URL into the `BASE` line of your `.env` so it's set in new terminals too.

## Redeploying code changes

For changes under `src/` only, repackage and update the function code. There's no need to redeploy the stack:

```bash
rm -rf package/src function.zip
cp -r src package/
cd package && zip -r ../function.zip . && cd ..
aws s3 cp function.zip s3://$S3_BUCKET/strands-demo/function.zip --region $AWS_REGION
aws lambda update-function-code \
  --function-name strands-demo-api-$ENV \
  --s3-bucket $S3_BUCKET \
  --s3-key strands-demo/function.zip \
  --region $AWS_REGION
aws lambda wait function-updated --function-name strands-demo-api-$ENV --region $AWS_REGION
```

- If you changed `requirements.txt`, re-run the full [step 1](#1-package-the-code) instead of the first three lines.
- If you changed a tool's parameters, reset the chat afterwards with `curl -X DELETE "$BASE/api/v1/agent/chat"`. The saved conversation contains tool calls in the old format, and Bedrock rejects them with an `invalid sequence as part of ToolUse` error.

## Seed demo data

The seed scripts use `boto3`, which is installed in the test virtualenv (see [Running the tests](#running-the-tests)):

```bash
.venv/bin/python scripts/seed_species.py --env $ENV --region $AWS_REGION
.venv/bin/python scripts/seed_demo_data.py --env $ENV --region $AWS_REGION
```

This creates the demo garden (`demo-garden-1`) with two plants, `tomato-01` and `basil-01`. If you changed the `DemoGardenId` or `DemoUserId` stack parameters, pass the same values with `--garden-id` and `--user-id`.

## API endpoints

`BASE` is the Function URL from [deploy step 5](#5-get-the-function-url). Replace `<instance_id>` with a plant's `instance_id` from the list response, for example `tomato-01`.

```bash
### REST endpoints

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

### Dashboard (session-open agent)

# Trigger an async agent run that refreshes the dashboard summary (returns 202 immediately)
curl -X POST "$BASE/api/v1/agent/session-open"

# Get the AI-generated dashboard summary
curl "$BASE/api/v1/dashboard/summary"

### Chat agent (synchronous: waits for the reply)

# Ask the garden agent a question
curl -X POST "$BASE/api/v1/agent/chat" \
  -H "Content-Type: application/json" \
  -d '{"message": "Which of my plants need watering today?"}'

# Continue the conversation (history is saved automatically per garden)
curl -X POST "$BASE/api/v1/agent/chat" \
  -H "Content-Type: application/json" \
  -d '{"message": "I just watered the basil"}'

# Reset the conversation and start fresh
curl -X DELETE "$BASE/api/v1/agent/chat"
```

## DynamoDB key scheme

```
PK=GARDEN#<id>   SK=METADATA
PK=GARDEN#<id>   SK=PLANT#<instance_id>
PK=GARDEN#<id>   SK=LOG#<instance_id>#<occurred_at>#<action_id>
PK=GARDEN#<id>   SK=DASHBOARD
PK=GARDEN#<id>   SK=CHAT_SESSION
PK=SPECIES#<id>  SK=METADATA
PK=USER#<id>     SK=PROFILE
```

## Teardown

> ⚠️ The Function URL is open to the public internet with no authentication. Delete the stack as soon as you are finished to avoid unexpected charges and exposure.

Delete the CloudFormation stack. This removes the Lambda, DynamoDB table, IAM role and Function URL in one go:

```bash
aws cloudformation delete-stack \
  --stack-name $STACK_NAME \
  --region $AWS_REGION
```

Wait for deletion to complete:

```bash
aws cloudformation wait stack-delete-complete \
  --stack-name $STACK_NAME \
  --region $AWS_REGION
```

The Lambda's CloudWatch log group isn't part of the stack, so delete it separately:

```bash
aws logs delete-log-group \
  --log-group-name /aws/lambda/strands-demo-api-$ENV \
  --region $AWS_REGION
```

If you created an S3 bucket specifically for this demo and no longer need it, empty and remove it too:

```bash
aws s3 rm s3://$S3_BUCKET/strands-demo/ --recursive
aws s3 rb s3://$S3_BUCKET
```

# GCP Setup Runbook: BigQuery + GCS

This runbook covers the Google Cloud setup behind this project: what exists, why each piece is configured the way it is, and the exact commands to rebuild it from scratch, check it, or tear it down.

> Commands use zsh/bash. Always wrap variables in braces (`"${P}:raw"`, not `$P:raw`). In zsh, `$P:r` is a path modifier and silently turns `stac-data-analytics-jp01:raw` into `stac-data-analytics-jp01aw`.

```bash
# Set these once per shell; every command below uses them.
P=stac-data-analytics-jp01                 # GCP project ID
B="gs://${P}-raw"                          # raw landing bucket
SA="stac-analytics-ingest@${P}.iam.gserviceaccount.com"
```

---

## 1. Resource inventory

| Resource | Type | Location | Purpose |
|---|---|---|---|
| `stac-data-analytics-jp01` | GCP project | — | Holds everything below. Billing is enabled. |
| `gs://stac-data-analytics-jp01-raw` | GCS bucket | `US` multi-region | Raw landing zone for STAC parquet and synthetic CSVs before they're loaded into BigQuery. |
| `raw` | BigQuery dataset | `US` | Raw tables loaded from GCS, 1:1 with the files. dbt reads these as **sources** and never writes here. |
| `satellite_analytics` | BigQuery dataset | `US` | Base dataset for the dbt **prod** target. Prod models go to `satellite_analytics_staging`, `_intermediate` and `_marts`. |
| `dbt_dev_*` | BigQuery datasets | `US` | Created by dbt on the first `dbt run` against the **dev** target. Not created by hand. |
| `stac-analytics-ingest` | Service account | — | Non-human identity for loaders and CI. Least-privilege roles, **no keys**. |

Data flow:

```
STAC API ─┐
          ├─► local parquet/csv ─► gs://…-raw/<prefix>/ ─► BigQuery `raw` ─► dbt ─► *_staging ─► *_intermediate ─► *_marts ─► Looker Studio / Omni
synthetic ┘
```

All BigQuery datasets and the bucket share the `US` location. BigQuery can only load from and query across matching locations, so any new dataset must also be created in `US`.

---

## 2. Prerequisites

- **Google Cloud SDK** (`gcloud`, `bq`, `gsutil`). Installed at `~/Downloads/google-cloud-sdk`; version 586 at the time of writing.
- Logged in, with the project set as the default:

```bash
gcloud auth login                          # opens a browser; signs in the CLI as you
gcloud config set project "${P}"
gcloud config list                         # expect: account = you@example.com, project = stac-data-analytics-jp01
```

- The repo's Python environment: `pip install -r requirements-dev.txt` (ingestion + dbt 1.12 + pytest).

---

## 3. Project, billing and APIs

Billing is linked, so the project runs on the normal **free tier**, not the BigQuery sandbox. The sandbox expires tables after 60 days and blocks some DML. The free tier covers 1 TiB of queries and 10 GiB of storage per month, and this project should stay well inside both.

APIs this project relies on (all already enabled):

| API | Why |
|---|---|
| `bigquery.googleapis.com` | Datasets, tables, query and load jobs |
| `bigquerystorage.googleapis.com` | Fast reads into pandas and notebooks |
| `storage.googleapis.com` | The landing bucket |

To check, or to re-enable on a fresh project:

```bash
gcloud billing projects describe "${P}"            # billingEnabled: true
gcloud services enable bigquery.googleapis.com bigquerystorage.googleapis.com storage.googleapis.com
gcloud services list --enabled | grep -E 'bigquery|storage'
```

---

## 4. GCS landing bucket

### Settings

| Setting | Value | Why |
|---|---|---|
| Location | `US` multi-region | Matches the BigQuery datasets |
| Storage class | `STANDARD` | Small, frequently read files |
| Uniform bucket-level access | on | IAM only, no per-object ACLs |
| Public access prevention | **enforced** | The bucket can never be made public by accident |
| Soft delete | 7 days (default) | Accidentally deleted objects can be recovered |
| Lifecycle rules | none | The data is tiny and raw history is worth keeping |
| Labels | `project=stac-analytics`, `layer=raw` | Cost attribution in billing reports |

### Commands

```bash
# Create (already done; this is for rebuilding)
gcloud storage buckets create "${B}" --location=US --default-storage-class=STANDARD \
  --uniform-bucket-level-access --public-access-prevention

# Hardening + labels (applied to the existing bucket)
gcloud storage buckets update "${B}" --public-access-prevention \
  --update-labels=project=stac-analytics,layer=raw

gcloud storage buckets describe "${B}" --format='yaml(location,public_access_prevention,uniform_bucket_level_access,labels)'
```

### Object layout convention

The loaders write to Hive-style date partitions, so each run is immutable and can be replayed:

```
gs://stac-data-analytics-jp01-raw/
├── stac_items/ingest_date=YYYY-MM-DD/stac_items_*.parquet
├── synthetic/customers/ingest_date=YYYY-MM-DD/customers.csv
└── synthetic/orders/ingest_date=YYYY-MM-DD/orders.csv
```

`_smoke_test/` is reserved for throwaway verification files (see §9) and should always be empty.

---

## 5. BigQuery datasets

### Layout

| Dataset | Written by | Contents |
|---|---|---|
| `raw` | Python loaders | `stac_items`, `customers`, `orders`, 1:1 with the GCS files |
| `dbt_dev_staging` / `_intermediate` / `_marts` | `dbt run` (dev target, default) | Your development builds |
| `satellite_analytics_staging` / `_intermediate` / `_marts` | `dbt run --target prod` | The "published" models that dashboards read |
| `satellite_analytics` | — | dbt prod base dataset. Stays empty because every model sets a custom `+schema`. |

dbt's default naming joins the target dataset and the layer's custom schema (`<target dataset>_<layer>`). That's why only `raw` and `satellite_analytics` are created by hand; dbt creates the rest.

### Commands

```bash
# Raw landing dataset
bq --location=US mk --dataset \
  --description "Raw landing tables loaded from GCS; read by dbt as sources. Not modified by dbt." \
  --label project:stac-analytics --label layer:raw \
  "${P}:raw"

# dbt prod base dataset (already existed; description and labels added)
bq update \
  --description "Base dataset for the dbt prod target; dbt builds satellite_analytics_staging / _intermediate / _marts alongside it." \
  --set_label project:stac-analytics --set_label layer:dbt-prod \
  "${P}:satellite_analytics"

bq ls --project_id "${P}"
bq show --format=prettyjson "${P}:raw" | grep -E '"location"|description'
```

---

## 6. IAM

### Who has what

| Principal | Role | Scope | Why |
|---|---|---|---|
| `user:you@example.com` | `roles/owner` | Project | You. Used locally through ADC (§7). |
| `stac-analytics-ingest` SA | `roles/bigquery.dataEditor` | Project | Create and overwrite tables in `raw`; lets dbt create its `*_staging`/`*_marts` datasets (includes `bigquery.datasets.create`) |
| `stac-analytics-ingest` SA | `roles/bigquery.jobUser` | Project | Run load and query jobs (dataEditor alone can't start jobs) |
| `stac-analytics-ingest` SA | `roles/storage.objectAdmin` | **Bucket only** | Read and write landing files. It gets no access to any other bucket. |

The service account has **no keys**. Locally you authenticate as yourself. CI will use Workload Identity Federation (§12), which exchanges GitHub's OIDC token for short-lived credentials, so there's never a long-lived JSON key to leak. `.gitignore` still blocks `*-key.json`, `gcp-service-account*.json` and `credentials/` as a backstop.

### Commands (for rebuilding)

```bash
gcloud iam service-accounts create stac-analytics-ingest --display-name="STAC Data Analytics Ingestion"

gcloud projects add-iam-policy-binding "${P}" --member="serviceAccount:${SA}" --role=roles/bigquery.dataEditor --condition=None
gcloud projects add-iam-policy-binding "${P}" --member="serviceAccount:${SA}" --role=roles/bigquery.jobUser --condition=None
gcloud storage buckets add-iam-policy-binding "${B}" --member="serviceAccount:${SA}" --role=roles/storage.objectAdmin

# Inspect
gcloud projects get-iam-policy "${P}" --flatten=bindings --filter="bindings.members:${SA}" --format='value(bindings.role)'
gcloud storage buckets get-iam-policy "${B}"
gcloud iam service-accounts keys list --iam-account="${SA}" --managed-by=user   # expect: 0 items
```

---

## 7. Local authentication (Application Default Credentials)

`gcloud auth login` authenticates the **CLI**. Python client libraries and dbt (`method: oauth` in `dbt_project/profiles.yml`) don't use those credentials. They look for **Application Default Credentials** instead, which are set up separately:

```bash
gcloud auth application-default login                              # opens a browser
gcloud auth application-default set-quota-project "${P}"           # bills API quota to this project
ls ~/.config/gcloud/application_default_credentials.json           # should exist now
```

ADC is per machine and stays out of the repo. In CI, `GOOGLE_APPLICATION_CREDENTIALS` provides the same thing.

### Optional: test as the service account (least-privilege check)

This confirms the SA alone can do the loader's job, so nothing quietly depends on your owner role:

```bash
# One-time: let yourself mint short-lived tokens for the SA
gcloud iam service-accounts add-iam-policy-binding "${SA}" \
  --member="user:you@example.com" --role=roles/iam.serviceAccountTokenCreator

# Then any CLI command can run as the SA
gcloud storage ls "${B}" --impersonate-service-account="${SA}"
# bq has no per-command flag; set impersonation globally, run bq, then unset it
gcloud config set auth/impersonate_service_account "${SA}"
bq ls --project_id "${P}"
gcloud config unset auth/impersonate_service_account

# Or make ADC itself impersonate the SA (affects dbt and Python), and switch back afterwards
gcloud auth application-default login --impersonate-service-account="${SA}"
gcloud auth application-default login                               # back to yourself
```

---

## 8. Cost guardrails

The project should cost close to $0. These guardrails make sure a bug can't change that.

| Guardrail | Where | Setting |
|---|---|---|
| Per-query cap | `dbt_project/profiles.yml` | `maximum_bytes_billed: 1000000000` (1 GB). Any dbt query that would scan more fails. |
| Daily query quota | Console → **IAM & Admin → Quotas & system limits** → filter `BigQuery API` / **"Query usage per day"** → Edit | ~100 GiB/day. Caps the whole project, including ad-hoc console queries. |
| Billing budget | Console → **Billing → Budgets & alerts → Create budget** | Scope: this project. Amount: $10/month. Email alerts at 50%, 90% and 100% of actual spend. |
| Ad-hoc queries | BigQuery console | Check the "This query will process X" estimate before running. Prefer `SELECT` with explicit columns over `SELECT *`. |

A budget **alerts**; it doesn't stop spending. The quota and `maximum_bytes_billed` are the hard stops.

**GCS free-tier note:** the always-free 5 GB of GCS storage only applies to *regional* buckets in `us-east1`, `us-west1` and `us-central1`. This bucket is in the `US` multi-region, so storage is billed at about $0.026/GB-month. At this project's data size (a few MB) that's well under a cent per month, so moving the bucket isn't worth it.

---

## 9. Verification (end-to-end smoke test)

These commands prove the full GCS → BigQuery path works, using the committed sample, before any loader code exists. Run them from the repo root.

```bash
gcloud storage cp data/raw_sample/stac_items_sample.parquet "${B}/_smoke_test/"
bq load --source_format=PARQUET "${P}:raw._smoke_test" "${B}/_smoke_test/stac_items_sample.parquet"
bq query --use_legacy_sql=false --format=pretty \
  "SELECT region, COUNT(*) AS n FROM \`${P}.raw._smoke_test\` GROUP BY region ORDER BY region"

# Clean up
bq rm -f -t "${P}:raw._smoke_test"
gcloud storage rm -r "${B}/_smoke_test/"
```

**Expected output (2026-10-17 run):** 6 regions × 83 rows = 498 rows (`amazon`, `middle_east`, `sahel`, `southeast_asia`, `us_midwest`, `western_europe`). The counts matched `pandas.read_parquet` on the same file exactly.

**dbt connection check** (needs ADC from §7):

```bash
cd dbt_project
dbt debug            # expect: "All checks passed!"
```

dbt picks up `GCP_PROJECT_ID` and the other variables from the repo-root `.env`, with no `source` needed.

**State check:**

```bash
bq ls --project_id "${P}"                                       # raw, satellite_analytics (+ dbt_dev_* after a dbt run)
gcloud storage buckets describe "${B}" --format='value(public_access_prevention)'   # enforced
```

---

## 10. Environment variables (`.env`)

`.env` lives at the repo root and is gitignored. `.env.example` is the committed template.

| Variable | Value here | Used by |
|---|---|---|
| `GCP_PROJECT_ID` | `stac-data-analytics-jp01` | Loaders, dbt (required) |
| `GCS_BUCKET_NAME` | `stac-data-analytics-jp01-raw` | Loaders |
| `BIGQUERY_RAW_DATASET` | `raw` | Loaders (write), dbt sources (read) |
| `BIGQUERY_DATASET` | `satellite_analytics` | dbt prod target base dataset |
| `BIGQUERY_LOCATION` | `US` | dbt, loaders |
| `DBT_DEV_DATASET` | `dbt_dev` (default) | dbt dev target base dataset |
| `DBT_TARGET` | `dev` (default) | dbt |
| `STAC_API_URL` | Planetary Computer | STAC pull |

None of these are secrets. Credentials come from ADC, never from `.env`.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `dbt debug` → `'NoneType' object has no attribute 'close'` or `DefaultCredentialsError` | No ADC on this machine | `gcloud auth application-default login` (§7) |
| Warning: "Your application has authenticated using end user credentials … without a quota project" | ADC has no quota project | `gcloud auth application-default set-quota-project "${P}"` |
| `Dataset name: stac-data-analytics-jp01aw is invalid` | zsh's `:r` modifier mangled `$P:raw` | Write `"${P}:raw"` |
| `Not found: Dataset … was not found in location EU` (or similar) | Location mismatch between dataset, job or bucket | Keep everything in `US`. Pass `--location=US` to `bq` if needed. |
| `Env var required but not provided: 'GCP_PROJECT_ID'` | dbt not run from `dbt_project/`, or `.env` missing | `cd dbt_project`. Check `.env` exists at the repo root, or `set -a; source ../.env; set +a`. |
| `Query exceeded limit for bytes billed` | `maximum_bytes_billed` guardrail tripped | Expected behaviour. Narrow the query (columns, partitions) instead of raising the cap. |
| `403 … does not have bigquery.jobs.create` when impersonating the SA | SA lost `bigquery.jobUser` | Re-add the binding (§6) |

---

## 12. Deferred: CI authentication and BI access

### Workload Identity Federation for GitHub Actions (to be built in the CI step)

Goal: `.github/workflows/ci.yml` runs `dbt test` against BigQuery as `stac-analytics-ingest`, with no JSON key stored anywhere.

```bash
PROJECT_NUMBER=$(gcloud projects describe "${P}" --format='value(projectNumber)')   
REPO=julianpelzner/STAC-data-analytics

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com

gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github-actions \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${REPO}'"

gcloud iam service-accounts add-iam-policy-binding "${SA}" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"
```

Workflow side:

```yaml
permissions:
  contents: read
  id-token: write          # lets the job request a GitHub OIDC token

steps:
  - uses: actions/checkout@v4
  - uses: google-github-actions/auth@v2
    with:
      workload_identity_provider: projects/982493846054/locations/global/workloadIdentityPools/github/providers/github-actions
      service_account: stac-analytics-ingest@stac-data-analytics-jp01.iam.gserviceaccount.com
  # The auth action exports GOOGLE_APPLICATION_CREDENTIALS, which dbt's method: oauth picks up.
```

The `attribute-condition` limits access to tokens from this one repo, so no other GitHub repository can assume the SA.

### Looker Studio / Omni (to be done in the BI step)

Dashboards should read only the `satellite_analytics_marts` dataset. Grant the BI identity `roles/bigquery.dataViewer` on that dataset (not the whole project) plus `roles/bigquery.jobUser` on the project. For the public Looker Studio link, use **owner's credentials** as the data source credential, so viewers don't need GCP access.

---

## 13. Teardown

This is irreversible: it deletes all data in the project's BigQuery datasets and bucket.

```bash
for ds in $(bq ls --project_id "${P}" --format=json | python3 -c 'import json,sys;print(" ".join(d["datasetReference"]["datasetId"] for d in json.load(sys.stdin)))'); do
  bq rm -r -f -d "${P}:${ds}"
done
gcloud storage rm -r "${B}"
gcloud iam service-accounts delete "${SA}"
# Or remove everything at once (30-day recovery window):
# gcloud projects delete "${P}"
```

---

## Change log

| Date | Change |
|---|---|
| 2026-09-22 | Project, billing, bucket, `satellite_analytics` dataset and service account created (Console) |
| 2026-10-17 | `raw` dataset created; bucket public access prevention set to enforced; labels added; smoke test passed; this runbook written |

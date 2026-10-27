# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

locals {
  # Enabled first, through the api_bootstrap provider: the provider needs both
  # to manage anything else in a project.
  bootstrap_services = [
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]

  cicd_services = [
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "aiplatform.googleapis.com",
{%- if cookiecutter.supports_bq_analytics %}
    "bigquery.googleapis.com",
{%- endif %}
    "cloudtrace.googleapis.com",
    "telemetry.googleapis.com",
{%- if cookiecutter.session_type == "cloud_sql" %}
    "sqladmin.googleapis.com",
{%- endif %}
  ]

  deploy_project_services = [
    "aiplatform.googleapis.com",
{%- if cookiecutter.deployment_target != 'gke' %}
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
{%- endif %}
{%- if cookiecutter.deployment_target == "gke" %}
    "compute.googleapis.com",
    "container.googleapis.com",
{%- endif %}
    "iam.googleapis.com",
{%- if cookiecutter.supports_bq_analytics %}
    "bigquery.googleapis.com",
{%- endif %}
    "logging.googleapis.com",
    "cloudtrace.googleapis.com",
    "telemetry.googleapis.com",
{%- if cookiecutter.session_type == "cloud_sql" %}
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com"
{%- endif %}
  ]

  deploy_project_ids = {
    prod    = var.prod_project_id
    staging = var.staging_project_id
  }

  all_project_ids = [
    var.cicd_runner_project_id,
    var.prod_project_id,
    var.staging_project_id
  ]

}


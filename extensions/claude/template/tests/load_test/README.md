# Load testing

[Locust](http://locust.io) drives the A2A `message/stream` route this project serves.

Local, against `uv run uvicorn app.fast_api_app:app --port 8000`:

```bash
uv run --with locust locust -f tests/load_test/load_test.py -H http://127.0.0.1:8000 \
    -u 10 -r 2 -t 30s --headless
```

Against a deployed service, send an identity token and point at its URL:

```bash
export _ID_TOKEN=$(gcloud auth print-identity-token -q)
uv run --with locust locust -f tests/load_test/load_test.py -H "$RUN_SERVICE_URL" \
    -u 10 -r 2 -t 60s --headless --csv=.results/report
```

On Agent Runtime, run it from the project root after `agents-cli deploy`, with
`_AUTH_TOKEN=$(gcloud auth print-access-token -q)` and no `-H`: it reads the agent from
`deployment_metadata.json`.

`--csv` writes the latency and failure tables Locust prints at the end.

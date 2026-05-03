# Copy Common Analysis Recipes

These short recipes cover the most common ways to queue and tune analysis runs from `jji` or `curl`.

> **Tip:** For the full flag list, request fields, and reusable defaults, see [CLI Command Reference](cli-command-reference.html), [REST API Reference](rest-api-reference.html), and [Configuration and Environment Reference](configuration-and-environment-reference.html).


> **Note:** `GET /results/{job_id}` returns `202` while a job is still `pending`, `waiting`, or `running`.

## Queue a Jenkins analysis from the CLI

Use this when you want to kick off analysis now and poll for the final result from a shell script.

```bash
server=https://jji.example.com

job_id="$(
  jji --server "$server" --user alex --json analyze \
    --job-name qe/nightly/api-tests \
    --build-number 1842 \
    --provider claude \
    --model opus-4 \
    --jenkins-url https://jenkins.ci.example.com \
    --jenkins-user svc-jji \
    --jenkins-password jenkins-api-token-123 \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])'
)"

while :; do
  result="$(jji --server "$server" --user alex --json status "$job_id")"
  status="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$result")"
  [ "$status" = "completed" ] || [ "$status" = "failed" ] && break
  sleep 15
done

printf '%s\n' "$result"
```

This submits `jji analyze`, captures the returned `job_id`, and polls until the job reaches a terminal state. Use it for terminal automation, cron jobs, or wrapper scripts that need the final JSON document instead of the initial queued response.

- Use `jji --server "$server" --user alex status "$job_id"` if you only want a quick human-readable status check.

## Queue a Jenkins analysis from the REST API

Use this when you want the same background flow from `curl` or another HTTP client.

```bash
server=https://jji.example.com

response="$(
  curl -sS \
    --cookie "jji_username=alex" \
    --header 'Content-Type: application/json' \
    --data '{"job_name":"qe/nightly/api-tests","build_number":1842,"ai_provider":"claude","ai_model":"opus-4","jenkins_url":"https://jenkins.ci.example.com","jenkins_user":"svc-jji","jenkins_password":"jenkins-api-token-123"}' \
    "$server/analyze"
)"

job_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<<"$response")"

while :; do
  result="$(curl -sS --cookie "jji_username=alex" "$server/results/$job_id")"
  status="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$result")"
  [ "$status" = "completed" ] || [ "$status" = "failed" ] && break
  sleep 15
done

printf '%s\n' "$result"
```

This posts to `/analyze`, saves the queued `job_id`, and polls `/results/{job_id}` until the run finishes. Use it when you want a copy-pasteable API-only flow without relying on a saved CLI profile.

## Wait for Jenkins to finish before analysis from the CLI

Use this when the Jenkins build is still running and you want JJI to wait before it starts analysis.

```bash
jji --server https://jji.example.com --user alex analyze \
  --job-name qe/nightly/api-tests \
  --build-number 1842 \
  --provider claude \
  --model opus-4 \
  --jenkins-url https://jenkins.ci.example.com \
  --jenkins-user svc-jji \
  --jenkins-password jenkins-api-token-123 \
  --wait \
  --poll-interval 2 \
  --max-wait 90
```

This queues the job immediately, then keeps it in the server-side `waiting` state until Jenkins reports that the build is done. Use it when your analysis request may arrive before test execution has completed.

- Set `--max-wait 0` to wait indefinitely.
- `--poll-interval` is in minutes.

## Wait for Jenkins to finish before analysis from the REST API

Use this when you need the same Jenkins-wait behavior from an HTTP request.

```bash
curl -sS \
  --cookie "jji_username=alex" \
  --header 'Content-Type: application/json' \
  --data '{"job_name":"qe/nightly/api-tests","build_number":1842,"ai_provider":"claude","ai_model":"opus-4","jenkins_url":"https://jenkins.ci.example.com","jenkins_user":"svc-jji","jenkins_password":"jenkins-api-token-123","wait_for_completion":true,"poll_interval_minutes":2,"max_wait_minutes":90}' \
  https://jji.example.com/analyze
```

This sends the wait settings directly in the `/analyze` body, so the queued job pauses in JJI until Jenkins finishes or the timeout is reached. It is useful for chatops bots, webhooks, or scripts that submit analysis as soon as a build is triggered.

- Set `"max_wait_minutes": 0` to wait with no deadline.

## Add peer review from the CLI

Use this when you want the primary model to debate the result with one or more peer models.

```bash
jji --server https://jji.example.com --user alex analyze \
  --job-name qe/nightly/api-tests \
  --build-number 1842 \
  --provider claude \
  --model opus-4 \
  --jenkins-url https://jenkins.ci.example.com \
  --jenkins-user svc-jji \
  --jenkins-password jenkins-api-token-123 \
  --peers "cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro" \
  --peer-analysis-max-rounds 5
```

This runs the main analysis with `claude:opus-4` and sends the result through peer-review rounds with the listed peer models. Use it for noisy failures, release blockers, or any run where you want more than one model to challenge the first answer.

- `--peer-analysis-max-rounds` accepts values from `1` to `10`.

## Add peer review from the REST API

Use this when you want peer analysis from `curl` or an API client.

```bash
curl -sS \
  --cookie "jji_username=alex" \
  --header 'Content-Type: application/json' \
  --data '{"job_name":"qe/nightly/api-tests","build_number":1842,"ai_provider":"claude","ai_model":"opus-4","jenkins_url":"https://jenkins.ci.example.com","jenkins_user":"svc-jji","jenkins_password":"jenkins-api-token-123","peer_ai_configs":[{"ai_provider":"cursor","ai_model":"gpt-5.4-xhigh"},{"ai_provider":"gemini","ai_model":"gemini-2.5-pro"}],"peer_analysis_max_rounds":5}' \
  https://jji.example.com/analyze
```

This sends the peer-review models as `peer_ai_configs` and limits the debate to five rounds. Use it when your automation layer works directly against the API and you want consistent multi-model review behavior.

- Send `"peer_ai_configs": []` to disable a server default just for one request.

## Add extra repository context from the CLI

Use this when the failure only makes sense if the AI can inspect related repos alongside the test repo.

```bash
jji --server https://jji.example.com --user alex analyze \
  --job-name qe/nightly/api-tests \
  --build-number 1842 \
  --provider claude \
  --model opus-4 \
  --jenkins-url https://jenkins.ci.example.com \
  --jenkins-user svc-jji \
  --jenkins-password jenkins-api-token-123 \
  --tests-repo-url https://github.com/acme/api-tests.git:release-4.18 \
  --additional-repos "infra:https://github.com/acme/infra.git:release-4.18,product:https://github.com/acme/product.git:main"
```

This clones the main test repo and the named extra repos into one shared workspace before analysis starts. Use it when the failure depends on product code, deployment code, or shared infrastructure that lives outside the test repo.

- Keep extra repo names short and stable, such as `infra` or `product`, because those names become workspace directory names.
- For a private extra repo on the CLI, append `@token`, for example `infra:https://github.com/acme/infra.git:release-4.18@ghp_exampleinfra123`.

## Add extra repository context from the REST API

Use this when you need the same multi-repo workspace through the API.

```bash
curl -sS \
  --cookie "jji_username=alex" \
  --header 'Content-Type: application/json' \
  --data '{"job_name":"qe/nightly/api-tests","build_number":1842,"ai_provider":"claude","ai_model":"opus-4","jenkins_url":"https://jenkins.ci.example.com","jenkins_user":"svc-jji","jenkins_password":"jenkins-api-token-123","tests_repo_url":"https://github.com/acme/api-tests.git:release-4.18","additional_repos":[{"name":"infra","url":"https://github.com/acme/infra.git","ref":"release-4.18"},{"name":"product","url":"https://github.com/acme/product.git","ref":"main"}]}' \
  https://jji.example.com/analyze
```

This sends the extra repos as structured `additional_repos` entries, each with its own name and optional ref. Use it when your analysis caller already has repo metadata and you want to pass it directly instead of encoding it into one CLI string.

- Add `"token":"ghp_exampleinfra123"` to an extra repo object when the clone needs token-based access.
- Send `"additional_repos": []` to disable a server default for one request.

## Analyze against a self-signed Jenkins from the CLI

Use this when JJI can reach Jenkins but certificate validation fails against an internal or self-signed TLS endpoint.

```bash
jji --server https://jji.example.com --user alex analyze \
  --job-name qe/nightly/api-tests \
  --build-number 1842 \
  --provider claude \
  --model opus-4 \
  --jenkins-url https://jenkins.ci.example.com \
  --jenkins-user svc-jji \
  --jenkins-password jenkins-api-token-123 \
  --no-jenkins-ssl-verify
```

This disables certificate verification only for the Jenkins API calls used by this analysis request. Use it for one-off runs against internal Jenkins instances with self-signed or privately issued certificates.

- If the JJI server itself has a self-signed cert, use the CLI's global `--insecure` or `--no-verify-ssl` option instead.

## Analyze against a self-signed Jenkins from the REST API

Use this when you need the same Jenkins TLS override in an API request.

```bash
curl -sS \
  --cookie "jji_username=alex" \
  --header 'Content-Type: application/json' \
  --data '{"job_name":"qe/nightly/api-tests","build_number":1842,"ai_provider":"claude","ai_model":"opus-4","jenkins_url":"https://jenkins.ci.example.com","jenkins_user":"svc-jji","jenkins_password":"jenkins-api-token-123","jenkins_ssl_verify":false}' \
  https://jji.example.com/analyze
```

This passes `jenkins_ssl_verify: false` in the request body so the server skips Jenkins certificate verification for this run. It is the API equivalent of `--no-jenkins-ssl-verify`.

## Force analysis for a build that passed from the CLI

Use this when Jenkins says the build succeeded but you still want JJI to inspect it.

```bash
jji --server https://jji.example.com --user alex analyze \
  --job-name qe/nightly/api-tests \
  --build-number 1842 \
  --provider claude \
  --model opus-4 \
  --jenkins-url https://jenkins.ci.example.com \
  --jenkins-user svc-jji \
  --jenkins-password jenkins-api-token-123 \
  --force
```

By default, JJI returns early on `SUCCESS` builds with a short "Build passed successfully" summary and no failure analysis. `--force` bypasses that early return so you can inspect suspicious passes, flaky-success cases, or unexpected console output.

- If you want this behavior as a reusable default, move it into server configuration or environment defaults described in [Configuration and Environment Reference](configuration-and-environment-reference.html).

## Force analysis for a build that passed from the REST API

Use this when you want the same passed-build override from an HTTP request.

```bash
curl -sS \
  --cookie "jji_username=alex" \
  --header 'Content-Type: application/json' \
  --data '{"job_name":"qe/nightly/api-tests","build_number":1842,"ai_provider":"claude","ai_model":"opus-4","jenkins_url":"https://jenkins.ci.example.com","jenkins_user":"svc-jji","jenkins_password":"jenkins-api-token-123","force":true}' \
  https://jji.example.com/analyze
```

This sets `force: true` on the `/analyze` request so the server keeps going even when Jenkins reports `SUCCESS`. Use it when a passing build still needs AI inspection for debugging or audit reasons.

## Related Pages

- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [Customize AI Analysis](customize-ai-analysis.html)
- [CLI Command Reference](cli-command-reference.html)
- [REST API Reference](rest-api-reference.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)
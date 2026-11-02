# grafana-ds-export

Turn the data sources you set up in the Grafana UI into a provisioning file (`provisioning/datasources/*.yaml`) that Grafana loads as-is.

Written for [grafana/grafana#82851](https://github.com/grafana/grafana/issues/82851): "make the datasources on the UI exportable to a yml file". One Python file, standard library only (Python 3.8+), no install.

## Use

```bash
# service account token with the Admin role (Administration > Users and access > Service accounts)
export GRAFANA_TOKEN=glsa_xxx
python3 grafana_ds_export.py --url http://localhost:3000 -o datasources.yaml
```

Or with a user: `--user admin --password ...`. Other options:

| Option | What it does |
|---|---|
| `--org-id N` (repeatable) / `--all-orgs` | Export other organisations (`--all-orgs` needs a server admin user) |
| `--name "Loki"` (repeatable) | Only these data sources |
| `--skip-read-only` | Leave out data sources that are already provisioned |
| `--editable` | Write `editable: true` so they stay editable in the UI |
| `--secret-style env\|env-func\|file` | Secrets as `${VAR}` (default), `$__env{VAR}`, or `$__file{DIR/VAR}` |

## What it gets right that `curl | jq | yq` doesn't

1. **Secrets.** The API never returns passwords or tokens, only which secure fields are set. Each one becomes a placeholder such as `password: ${GRAFANA_DS_WAREHOUSE_PASSWORD}`, and the file header lists every variable to set. If two data sources would get the same variable name (say "Prometheus" in two orgs), the second one gets its uid appended, so no two data sources share a secret.
2. **Dollar signs.** Grafana expands `$VAR` and `${VAR}` in provisioning files. So a Loki derived-field link like `http://tempo/trace/${__value.raw}` silently turns into `http://tempo/trace/`. The exporter writes every literal `$` as `$$`, Grafana's escape.
3. **Clean output.** API-only fields (`id`, `typeLogoUrl`, `readOnly`, empty strings, false defaults) are dropped. `uid` and `orgId` are kept, so dashboards that reference the data source by uid keep working.

## A Grafana quirk to know about: `$` inside a secret

Grafana expands a secret's value twice: once to substitute the variable, then again over the result (`pkg/services/provisioning/values/values.go`, `interpolateValue`). So a password like `s3cr$t` arrives as `s3cr`. This happens with `${VAR}`, `$__env{}` and `$__file{}` alike, and `$$` inside the value doesn't help. What works (tested on Grafana 13.3.0): write each `$` in the secret as `${GRAFANA_DOLLAR}`, and also set `GRAFANA_DOLLAR='$'` for Grafana.

## Tested

- `tests/test_unit.py`: mapping, placeholders, dollar escaping, YAML output (`python3 -m unittest discover -s tests`).
- `tests/e2e_real_grafana.py /path/to/grafana`: starts a real Grafana (13.3.0 on 2026-11-01) and creates Prometheus (basic auth plus a secret containing `$`, exemplar link with `${__value.raw}`), Loki (derived fields), PostgreSQL (password) and TestData through the API. It exports them with both basic auth and a service account token, starts a second Grafana with only the exported file, and checks that every field, jsonData and set-secret list matches. It also checks the provisioned Prometheus password really reaches the backend. Run log: `tests/e2e-run-2026-11-01.log`.

## Limits

- Data sources only (dashboards and alerting have their own export in Grafana).
- Secret values have to be supplied again: the API can't read them back, by design.

Built and maintained by AI agents (thousand-ships). Not affiliated with Grafana Labs. Issues and requests are welcome. MIT licence.

#!/usr/bin/env python3
"""Export Grafana data sources to a provisioning file (provisioning/datasources/*.yaml).

Reads data sources through the Grafana HTTP API and writes YAML that Grafana's
file provisioning accepts as-is. Python 3.8+, standard library only.

Two things the usual `curl | jq | yq` route gets wrong, handled here:
  * Secrets. The API never returns password/token values (only which secure
    fields are set), so each one becomes an environment variable placeholder,
    e.g. `password: ${GRAFANA_DS_POSTGRES_PASSWORD}`, listed at the top of the file.
  * Dollar signs. Grafana expands `$VAR` and `${VAR}` in provisioning files, so a
    literal `$` (Loki derived-field links like `${__value.raw}`, Prometheus
    exemplar URLs) must be written as `$$` or it silently becomes empty.
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

__version__ = "0.1.1"

# Order of keys in the output, following Grafana's provisioning docs.
FIELD_ORDER = [
    "name", "type", "uid", "orgId", "access", "url", "user", "database",
    "basicAuth", "basicAuthUser", "withCredentials", "isDefault",
    "jsonData", "secureJsonData", "version", "editable",
]
# Fields that are left out when they hold their default/empty value.
OMIT_IF_EMPTY = {"user", "database", "basicAuthUser", "url"}
OMIT_IF_FALSE = {"basicAuth", "withCredentials", "isDefault"}


class GrafanaError(Exception):
    pass


class Client:
    def __init__(self, url, token=None, user=None, password=None, timeout=30):
        self.base = url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        if token:
            self.headers["Authorization"] = "Bearer " + token
        elif user is not None:
            raw = ("%s:%s" % (user, password or "")).encode()
            self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()

    def get(self, path, org_id=None):
        headers = dict(self.headers)
        if org_id is not None:
            headers["X-Grafana-Org-Id"] = str(org_id)
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            hint = ""
            if e.code in (401, 403):
                hint = (" (check the token: data source export needs a service account "
                        "with the Admin role, or a Grafana admin user)")
            raise GrafanaError("GET %s returned HTTP %s%s: %s" % (path, e.code, hint, body))
        except urllib.error.URLError as e:
            raise GrafanaError("could not reach %s: %s" % (self.base, e.reason))


def env_name(prefix, ds_name, field):
    """GRAFANA_DS_<NAME>_<FIELD>, upper case, only A-Z 0-9 _."""
    def clean(s):
        s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()
        return s or "X"
    name = "%s_%s_%s" % (prefix, clean(ds_name), clean(field))
    if name[0].isdigit():
        name = "_" + name
    return name


def escape_dollars(value):
    """Escape `$` as `$$` in every string, recursively, so Grafana keeps it literal."""
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [escape_dollars(v) for v in value]
    if isinstance(value, dict):
        return {k: escape_dollars(v) for k, v in value.items()}
    return value


def to_provisioning(ds, env_prefix="GRAFANA_DS", editable=None, keep_version=False,
                    secret_style="env", secret_dir="/etc/grafana/secrets", taken=None):
    """Map one data source from GET /api/datasources/uid/:uid to a provisioning entry.

    Returns (entry, env_vars) where env_vars maps placeholder name -> description.
    `taken` (a set shared across calls) keeps placeholder names unique: two data
    sources whose names clean to the same text (e.g. "Prometheus" in two orgs) get
    the uid appended instead of silently sharing one secret.
    """
    entry = {}
    for key in ("name", "type", "uid", "orgId", "access", "url", "user", "database",
                "basicAuth", "basicAuthUser", "withCredentials", "isDefault"):
        if key not in ds:
            continue
        val = ds[key]
        if key in OMIT_IF_EMPTY and (val is None or val == ""):
            continue
        if key in OMIT_IF_FALSE and not val:
            continue
        entry[key] = escape_dollars(val)
    json_data = ds.get("jsonData") or {}
    if json_data:
        entry["jsonData"] = escape_dollars(json_data)
    env_vars = {}
    secure = {}
    for field, is_set in sorted((ds.get("secureJsonFields") or {}).items()):
        if not is_set:
            continue
        var = env_name(env_prefix, ds.get("name", ""), field)
        if taken is not None:
            if var in taken:
                base = env_name(env_prefix, "%s_%s" % (ds.get("name", ""),
                                                      ds.get("uid") or ds.get("orgId") or ""), field)
                var, n = base, 2
                while var in taken:
                    var, n = "%s_%d" % (base, n), n + 1
            taken.add(var)
        # placeholder: not escaped, Grafana must expand it
        if secret_style == "file":
            secure[field] = "$__file{%s/%s}" % (secret_dir.rstrip("/"), var)
        elif secret_style == "env-func":
            secure[field] = "$__env{%s}" % var
        else:
            secure[field] = "${%s}" % var
        env_vars[var] = "%s: secureJsonData.%s" % (ds.get("name"), field)
    if secure:
        entry["secureJsonData"] = secure
    if keep_version and ds.get("version") is not None:
        entry["version"] = ds["version"]
    if editable is not None:
        entry["editable"] = bool(editable)
    ordered = {k: entry[k] for k in FIELD_ORDER if k in entry}
    return ordered, env_vars


# --- minimal YAML writer (strings are double-quoted JSON, which is valid YAML) ---

def _scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return json.dumps(str(v), ensure_ascii=False)


def _key(k):
    k = str(k)
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.\-]*$", k) and k.lower() not in (
            "true", "false", "null", "yes", "no", "on", "off", "y", "n"):
        return k
    return json.dumps(k, ensure_ascii=False)


def to_yaml(value, indent=0):
    pad = "  " * indent
    lines = []
    if isinstance(value, dict):
        if not value:
            return [pad + "{}"]
        for k, v in value.items():
            if isinstance(v, dict) and v:
                lines.append("%s%s:" % (pad, _key(k)))
                lines.extend(to_yaml(v, indent + 1))
            elif isinstance(v, list) and v:
                lines.append("%s%s:" % (pad, _key(k)))
                lines.extend(to_yaml(v, indent + 1))
            else:
                empty = "{}" if isinstance(v, dict) else "[]" if isinstance(v, list) else _scalar(v)
                lines.append("%s%s: %s" % (pad, _key(k), empty))
        return lines
    if isinstance(value, list):
        if not value:
            return [pad + "[]"]
        for item in value:
            if isinstance(item, (dict, list)) and item:
                sub = to_yaml(item, indent + 1)
                first = sub[0].lstrip()
                lines.append("%s- %s" % (pad, first))
                lines.extend(sub[1:])
            else:
                empty = "{}" if isinstance(item, dict) else "[]" if isinstance(item, list) else _scalar(item)
                lines.append("%s- %s" % (pad, empty))
        return lines
    return [pad + _scalar(value)]


def render(entries, env_vars, source_url):
    out = ["# Grafana data source provisioning file",
           "# Exported from %s by grafana-ds-export %s" % (source_url, __version__),
           "# Literal dollar signs are written as $$ (Grafana's escape for provisioning files)."]
    if env_vars:
        out.append("#")
        out.append("# Secrets are not readable through the API. Set these environment variables")
        out.append("# for the Grafana server before it starts. If a secret contains $, Grafana")
        out.append("# drops it (it expands the value a second time): write each $ as")
        out.append("# ${GRAFANA_DOLLAR} and also set GRAFANA_DOLLAR='$'.")
        for var, desc in env_vars.items():
            out.append("#   %s  (%s)" % (var, desc))
    out.append("apiVersion: 1")
    out.append("")
    doc = {"datasources": entries}
    out.extend(to_yaml(doc))
    return "\n".join(out) + "\n"


def collect(client, org_ids=None, names=None, skip_read_only=False):
    """Return full data source objects, one API call per data source."""
    result = []
    targets = org_ids if org_ids else [None]
    for org in targets:
        listing = client.get("/api/datasources", org_id=org)
        for item in listing:
            if names and item.get("name") not in names:
                continue
            uid = item.get("uid")
            full = client.get("/api/datasources/uid/%s" % uid, org_id=org) if uid else item
            if skip_read_only and full.get("readOnly"):
                continue
            result.append(full)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="grafana-ds-export",
        description="Export Grafana data sources to a provisioning YAML file.")
    p.add_argument("--url", default=os.environ.get("GRAFANA_URL", "http://localhost:3000"),
                   help="Grafana base URL (env GRAFANA_URL, default http://localhost:3000)")
    p.add_argument("--token", default=os.environ.get("GRAFANA_TOKEN"),
                   help="service account token with Admin role (env GRAFANA_TOKEN)")
    p.add_argument("--user", default=os.environ.get("GRAFANA_USER"),
                   help="basic auth user instead of a token (env GRAFANA_USER)")
    p.add_argument("--password", default=os.environ.get("GRAFANA_PASSWORD"),
                   help="basic auth password (env GRAFANA_PASSWORD)")
    p.add_argument("--org-id", type=int, action="append",
                   help="export this organisation (repeatable); default is the token's org")
    p.add_argument("--all-orgs", action="store_true",
                   help="export every organisation (needs a Grafana server admin user)")
    p.add_argument("--name", action="append", help="only export this data source (repeatable)")
    p.add_argument("--skip-read-only", action="store_true",
                   help="leave out data sources that are already provisioned")
    p.add_argument("--editable", action="store_true",
                   help="write editable: true so the data sources stay editable in the UI")
    p.add_argument("--keep-version", action="store_true", help="include the version field")
    p.add_argument("--env-prefix", default="GRAFANA_DS", help="prefix for secret placeholders")
    p.add_argument("--secret-style", choices=["env", "env-func", "file"], default="env",
                   help="how secrets are referenced: env = ${VAR}, env-func = $__env{VAR}, "
                        "file = $__file{DIR/VAR} (one file per secret)")
    p.add_argument("--secret-dir", default="/etc/grafana/secrets",
                   help="directory for --secret-style file (default /etc/grafana/secrets)")
    p.add_argument("-o", "--output", help="write to this file instead of stdout")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    if not args.token and args.user is None:
        p.error("give --token (service account token) or --user/--password")
    client = Client(args.url, token=args.token, user=args.user, password=args.password)
    try:
        org_ids = args.org_id
        if args.all_orgs:
            org_ids = [o["id"] for o in client.get("/api/orgs")]
        sources = collect(client, org_ids=org_ids, names=set(args.name or []),
                          skip_read_only=args.skip_read_only)
    except GrafanaError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    entries, env_vars, taken = [], {}, set()
    for ds in sources:
        entry, env = to_provisioning(ds, env_prefix=args.env_prefix,
                                     editable=True if args.editable else None,
                                     keep_version=args.keep_version,
                                     secret_style=args.secret_style, secret_dir=args.secret_dir,
                                     taken=taken)
        entries.append(entry)
        env_vars.update(env)
    if not entries:
        print("warning: no data sources matched (check --name, --org-id and the token's org)",
              file=sys.stderr)
    text = render(entries, env_vars, args.url)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print("wrote %d data source(s) to %s" % (len(entries), args.output), file=sys.stderr)
        if env_vars:
            print("set %d secret env var(s) before provisioning: %s"
                  % (len(env_vars), ", ".join(env_vars)), file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

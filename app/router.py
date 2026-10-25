"""Model routing: `provider/model` pins a provider; otherwise the route table
or any provider's discovered model list resolves it (lowest priority wins)."""
import difflib

from . import db


def _provider_rows():
    return [p for p in db.list_providers() if p["enabled"]]


def resolve(model):
    """Returns (provider_row, upstream_model) or raises KeyError with a hint."""
    providers = _provider_rows()
    if not providers:
        raise KeyError("没有启用的提供商，请先在控制台添加")
    by_id = {p["id"]: p for p in providers}

    if "/" in model:
        head, rest = model.split("/", 1)
        if head in by_id and rest:
            return by_id[head], rest

    routes = db.query(
        """SELECT m.local_name, m.provider_id, m.upstream_name FROM models m
           JOIN providers p ON p.id = m.provider_id
           WHERE m.local_name=? AND m.enabled=1 AND p.enabled=1
           ORDER BY p.priority, p.id""", (model,))
    if routes:
        r = routes[0]
        if r["provider_id"] in by_id:
            return by_id[r["provider_id"]], r["upstream_name"]

    for p in sorted(providers, key=lambda x: (x["priority"], x["id"])):
        if model in (p.get("models") or []):
            return p, model

    known = sorted({r["local_name"] for r in db.query("SELECT local_name FROM models WHERE enabled=1")})
    close = difflib.get_close_matches(model, known, n=5, cutoff=0.5)
    hint = f"，可用模型示例：{', '.join(close[:5])}" if close else ""
    raise KeyError(f"未找到模型 {model}{hint}。可用 provider/model 前缀指定提供商")


def list_local_models():
    """Local names visible at /v1/models, with owning provider."""
    rows = db.query(
        """SELECT m.local_name, m.provider_id, m.upstream_name FROM models m
           JOIN providers p ON p.id = m.provider_id
           WHERE m.enabled=1 AND p.enabled=1
           ORDER BY m.local_name, p.priority, p.id""")
    seen = {}
    for r in rows:
        seen.setdefault(r["local_name"], r)
    out = []
    for name in sorted(seen):
        r = seen[name]
        out.append({"id": name, "object": "model", "owned_by": r["provider_id"],
                    "created": 0, "upstream": r["upstream_name"]})
    return out


def sync_discovered(provider, model_ids):
    """Upsert route rows for a provider's discovered model list."""
    now_ids = set(model_ids)
    db.execute("DELETE FROM models WHERE provider_id=? AND source='discovered'", (provider["id"],))
    rows = [(m, provider["id"], m, 1, "discovered") for m in sorted(now_ids)]
    if rows:
        db.executemany(
            "INSERT OR IGNORE INTO models (local_name,provider_id,upstream_name,enabled,source) VALUES (?,?,?,?,?)",
            rows)

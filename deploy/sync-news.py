#!/usr/bin/env python3
"""Import news items from content.json into the site database.

content.json only seeds a database the first time it is created, so a news item
added to that file after the site is already running never reaches the live
database. This closes that gap: it inserts any news item in content.json whose
id is not in the database yet, and applies the same "only one featured item"
rule the admin API uses.

It is a dry run unless you pass --apply.

    sudo -u dmt-site env DMT_DB_PATH=/var/lib/dmt-class-site/class_site.sqlite3 \
      /opt/dmt-class-site/.venv/bin/python /opt/dmt-class-site/deploy/sync-news.py --apply
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server as core  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    args = parser.parse_args()

    content_path = ROOT / "content.json"
    items = json.loads(content_path.read_text(encoding="utf-8")).get("news", [])
    print("database     :", core.DB_PATH)
    print("table source :", content_path)
    print("news in file :", len(items))

    con = core.connect()
    existing = {row["id"] for row in con.execute("SELECT id FROM news")}
    missing = [item for item in items if item.get("id") not in existing]

    if not missing:
        print("nothing to do: every news item in content.json is already in the database")
        return 0

    print("not yet in the database:")
    for item in missing:
        print("   %-40s featured=%-5s %s" % (item.get("id"), item.get("featured"), item.get("title_cn")))

    if not args.apply:
        print()
        print("dry run: re-run with --apply to insert these items")
        return 0

    now = core.utc_now()
    for item in missing:
        if item.get("featured"):
            for row in con.execute("SELECT id, payload FROM news").fetchall():
                old = json.loads(row["payload"])
                if old.get("featured"):
                    old["featured"] = False
                    con.execute(
                        "UPDATE news SET payload=?, updated_at=? WHERE id=?",
                        (json.dumps(old, ensure_ascii=False), now, row["id"]),
                    )
        con.execute(
            "INSERT INTO news(id, payload, updated_at, updated_by) VALUES(?,?,?,?)",
            (item["id"], json.dumps(item, ensure_ascii=False), now, None),
        )
        con.execute(
            "INSERT INTO audit_log(actor_id, action, entity, entity_id, created_at) VALUES(?,?,?,?,?)",
            (None, "create", "news", item["id"], now),
        )
        print("inserted", item["id"])
    con.commit()
    con.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

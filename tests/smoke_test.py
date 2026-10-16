"""End-to-end smoke test against a running server.

    python tests/smoke_test.py path/to/sample.pst [--url http://localhost:8000] [--user admin --password ...]

Uploads the PST in chunks (deliberately re-sending one chunk and resuming mid-way), waits for
indexing, runs searches, opens an email, downloads an attachment, then deletes the archive.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pst")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.environ.get("APP_PASSWORD", ""))
    ap.add_argument("--query", default="operator balancing agreement")
    ap.add_argument("--keep", action="store_true", help="do not delete the archive afterwards")
    args = ap.parse_args()
    auth = (args.user, args.password) if args.password else None
    c = httpx.Client(base_url=args.url, auth=auth, timeout=120)

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            sys.exit(1)

    check(c.get("/healthz").json() == {"ok": True}, "healthz")
    if auth:
        check(httpx.get(args.url + "/api/archives").status_code == 401, "unauthenticated request rejected")

    data = open(args.pst, "rb").read()
    check(c.post("/api/uploads", json={"filename": "x.zip", "size": 10}).status_code == 400, "non-PST filename rejected")
    up = c.post("/api/uploads", json={"filename": os.path.basename(args.pst), "size": len(data)}).json()
    aid, chunk = up["id"], up["chunk_size"]
    print(f"upload {aid}: {len(data)} bytes in chunks of {chunk}")

    r = c.put(f"/api/uploads/{aid}", params={"offset": chunk}, content=data[chunk : 2 * chunk])
    check(r.status_code == 409, "non-contiguous chunk rejected")
    offset = 0
    while offset < len(data):
        piece = data[offset : offset + chunk]
        offset = c.put(f"/api/uploads/{aid}", params={"offset": offset}, content=piece).json()["bytes_received"]
        if offset == chunk:  # simulate a client retry of the same chunk, then a resume via GET
            c.put(f"/api/uploads/{aid}", params={"offset": 0}, content=data[:chunk])
            offset = c.get(f"/api/uploads/{aid}").json()["bytes_received"]
    check(offset == len(data), "all bytes received")
    check(c.post(f"/api/uploads/{aid}/complete").json()["status"] == "QUEUED", "upload completed and queued")

    deadline = time.time() + 600
    while time.time() < deadline:
        a = next(x for x in c.get("/api/archives").json() if x["id"] == aid)
        print(f"  {a['status']} {a['processed_items']}/{a['total_items']} items")
        if a["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(2)
    check(a["status"] == "COMPLETED", f"archive indexed ({a['processed_emails']} emails, {a['processed_attachments']} attachments, error={a['error_message']})")

    for mode in ("hybrid", "keyword", "semantic"):
        res = c.get("/api/search", params={"q": args.query, "mode": mode, "archive_id": aid}).json()
        check(res["count"] > 0, f"{mode} search returns {res['count']} results; top: {res['results'][0]['subject']!r}")
    res = c.get("/api/search", params={"q": args.query, "archive_id": aid, "source_type": "ATTACHMENT"}).json()
    check(all(r["source_type"] == "ATTACHMENT" for r in res["results"]), "source_type filter")
    res = c.get("/api/search", params={"q": args.query, "archive_id": aid, "date_from": "2100-01-01"}).json()
    check(res["count"] == 0, "date filter excludes everything in the future")
    res = c.get("/api/search", params={"q": "<script>alert(1)</script>", "archive_id": aid}).json()
    check(all("<script" not in r["snippet"] for r in res["results"]), "snippets are HTML-escaped")

    hit = c.get("/api/search", params={"q": args.query, "archive_id": aid, "source_type": "ATTACHMENT"}).json()["results"][0]
    email = c.get(f"/api/emails/{hit['email_id']}").json()
    check(email["subject"] == hit["subject"] and email["attachments"], f"email detail with {len(email['attachments'])} attachments")
    att = next(x for x in email["attachments"] if x["downloadable"])
    dl = c.get(f"/api/attachments/{att['id']}/download")
    check(dl.status_code == 200 and len(dl.content) == att["size_bytes"] and "attachment" in dl.headers["content-disposition"],
          f"attachment download {att['filename']} ({len(dl.content)} bytes)")

    if not args.keep:
        check(c.delete(f"/api/archives/{aid}").status_code == 200, "archive deleted")
        check(c.get(f"/api/emails/{hit['email_id']}").status_code == 404, "emails removed with archive")
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

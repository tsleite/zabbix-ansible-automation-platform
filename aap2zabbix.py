#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aap2zabbix.py
=============

Red Hat Ansible Automation Platform (AAP) -> Zabbix integration collector.

Polls the AAP Controller API for the most recently finished job and pushes
its status (and the full playbook stdout on failure) to Zabbix trapper
items using zabbix_sender.

Items expected on the monitored host (see templates/zbx_aap_template.yaml):
  - jobs.status   (unsigned, trapper)  1 = Successful / 0 = Failed
  - jobs.details  (text, trapper)      job header + full playbook stdout

All configuration is taken from environment variables — no credentials
are ever stored in this file. See .env.example.

Author : Tiago Silva Leite
License: MIT
"""

import json
import logging
import os
import subprocess
import sys
import tempfile

import requests
import urllib3

__version__ = "2.0.0"

# --------------------------------------------------------------------------- #
# Configuration (environment variables)
# --------------------------------------------------------------------------- #

ZABBIX_API_URL = os.environ.get("ZABBIX_API_URL")        # e.g. https://zabbix.example.com/api_jsonrpc.php
ZABBIX_API_TOKEN = os.environ.get("ZABBIX_API_TOKEN")    # API token (Administration > API tokens)
ZABBIX_SERVER = os.environ.get("ZABBIX_SERVER")          # Zabbix server/proxy address for zabbix_sender
ZABBIX_PORT = os.environ.get("ZABBIX_PORT", "10051")
ZABBIX_TARGET_HOST = os.environ.get("ZABBIX_TARGET_HOST")  # Host name as configured in Zabbix

AAP_API_URL = os.environ.get("AAP_API_URL")              # e.g. https://aap.example.com/api/v2
AAP_USERNAME = os.environ.get("AAP_USERNAME")
AAP_PASSWORD = os.environ.get("AAP_PASSWORD")

# Item IDs whose history is cleared on every run so "Latest data" always
# shows a single, current snapshot (comma separated list).
ZABBIX_CLEAR_ITEM_IDS = [
    i.strip() for i in os.environ.get("ZABBIX_CLEAR_ITEM_IDS", "").split(",") if i.strip()
]

VERIFY_TLS = os.environ.get("VERIFY_TLS", "true").lower() != "false"
ZBX_SENDER = os.environ.get("ZBX_SENDER", "/usr/bin/zabbix_sender")

ITEM_KEY_STATUS = "jobs.status"
ITEM_KEY_DETAILS = "jobs.details"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("aap2zabbix")

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    log.warning("TLS certificate verification is DISABLED (VERIFY_TLS=false).")


def _require_env() -> None:
    """Abort early if any mandatory environment variable is missing."""
    required = {
        "ZABBIX_API_URL": ZABBIX_API_URL,
        "ZABBIX_API_TOKEN": ZABBIX_API_TOKEN,
        "ZABBIX_SERVER": ZABBIX_SERVER,
        "ZABBIX_TARGET_HOST": ZABBIX_TARGET_HOST,
        "AAP_API_URL": AAP_API_URL,
        "AAP_USERNAME": AAP_USERNAME,
        "AAP_PASSWORD": AAP_PASSWORD,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(2)


# --------------------------------------------------------------------------- #
# Zabbix helpers
# --------------------------------------------------------------------------- #

def zabbix_api(method: str, params) -> dict:
    """Call the Zabbix JSON-RPC API using a Bearer API token (Zabbix >= 5.4)."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    response = requests.post(
        ZABBIX_API_URL,
        data=json.dumps(payload),
        headers={
            "Content-Type": "application/json-rpc",
            "Authorization": f"Bearer {ZABBIX_API_TOKEN}",
        },
        verify=VERIFY_TLS,
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if "error" in result:
        raise RuntimeError(f"Zabbix API error on {method}: {result['error']}")
    return result.get("result")


def clear_item_history() -> None:
    """Clear item history so Latest data always shows a single snapshot."""
    if not ZABBIX_CLEAR_ITEM_IDS:
        log.debug("ZABBIX_CLEAR_ITEM_IDS not set — skipping history.clear.")
        return
    try:
        zabbix_api("history.clear", ZABBIX_CLEAR_ITEM_IDS)
        log.info("Zabbix item history cleared: %s", ZABBIX_CLEAR_ITEM_IDS)
    except Exception as exc:  # noqa: BLE001 — history clear must never abort the send
        log.error("history.clear failed: %s", exc)


def zbx_send(key: str, value: str) -> None:
    """Send a value to a Zabbix trapper item via zabbix_sender --input-file.

    Using an input file (instead of interpolating the value on the command
    line) avoids shell-escaping issues and command injection with multi-line
    playbook output.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".zbx", delete=False) as fh:
        # Format: <host> <key> <value>  (value quoted, newlines escaped)
        escaped = value.replace('"', "'")
        fh.write(f'"{ZABBIX_TARGET_HOST}" {key} "{escaped}"\n')
        tmp_path = fh.name
    try:
        cmd = [
            ZBX_SENDER,
            "-z", ZABBIX_SERVER,
            "-p", str(ZABBIX_PORT),
            "--input-file", tmp_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            log.error("zabbix_sender failed (%s): %s", proc.returncode, proc.stderr.strip())
        else:
            log.info("Sent %s to Zabbix host '%s'.", key, ZABBIX_TARGET_HOST)
    finally:
        os.unlink(tmp_path)


# --------------------------------------------------------------------------- #
# AAP Controller helpers
# --------------------------------------------------------------------------- #

def aap_session() -> requests.Session:
    session = requests.Session()
    session.auth = (AAP_USERNAME, AAP_PASSWORD)
    session.verify = VERIFY_TLS
    return session


def get_last_job(session: requests.Session) -> dict | None:
    """Return the most recently finished job, or None if there are no jobs."""
    url = f"{AAP_API_URL.rstrip('/')}/jobs/?order_by=-finished&page_size=1"
    response = session.get(url, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("count", 0) == 0:
        return None
    return data["results"][0]


def get_job_stdout(session: requests.Session, job_id: int) -> str:
    """Download the full plain-text stdout of a job."""
    url = f"{AAP_API_URL.rstrip('/')}/jobs/{job_id}/stdout/?format=txt"
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def format_job_header(job: dict) -> str:
    return "\n".join(
        [
            f"Last Job ID: {job.get('id')}",
            f"Last Job Name: {job.get('name')}",
            f"Last Job Started: {job.get('started')}",
            f"Last Job finished: {job.get('finished')}",
            f"Last Job Status: {job.get('status')}",
            f"Last Job Playbook: {job.get('playbook')}",
            f"Last Job Description: {job.get('description')}",
        ]
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    _require_env()
    session = aap_session()

    try:
        job = get_last_job(session)
    except requests.exceptions.RequestException as exc:
        log.error("Failed to query AAP API: %s", exc)
        sys.exit(1)

    if job is None:
        log.info("No jobs found on the Automation Controller.")
        return

    status = str(job.get("status", "")).lower()
    log.info("Last job #%s '%s' finished with status: %s", job.get("id"), job.get("name"), status)

    # Keep Latest data as a single snapshot of the last execution.
    clear_item_history()

    if status == "failed":
        header = format_job_header(job)
        try:
            stdout = get_job_stdout(session, job["id"])
        except requests.exceptions.RequestException as exc:
            stdout = f"<unable to fetch job stdout: {exc}>"
        separator = "-" * 110
        zbx_send(ITEM_KEY_DETAILS, f"{header}\n\n{separator}\n{stdout}")
        zbx_send(ITEM_KEY_STATUS, "0")   # 0 = Failed -> fires the HIGH trigger
    else:
        zbx_send(ITEM_KEY_STATUS, "1")   # 1 = Successful

    session.close()


if __name__ == "__main__":
    main()

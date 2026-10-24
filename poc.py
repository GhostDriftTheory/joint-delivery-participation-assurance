#!/usr/bin/env python3
"""Joint Delivery Participation Assurance — synthetic, local reference PoC.
Copyright © 2026 AI Assurance, Inc. All rights reserved.
See README.md for scope, trust boundaries, and terms of use.
Python 3.10+; the core uses only the standard library.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 2_000_000
MAX_INT = 1_000_000_000
STATUSES = ("PASS", "HOLD", "REJECT")
EXPECTED_PROFILE_SHA256 = "7db4db5624c5475d1d2c80ee731ba5bffbc3cb90deeb12fa57ff98b1c1b7062f"
INPUT_SCHEMA = "joint-delivery-input/v1"
RECORD_SCHEMA = "joint-delivery-record/v1"
BUNDLE_SCHEMA = "joint-delivery-suite/v1"
PERMIT_SCHEMA = "joint-delivery-simulation-permit/v1"
COST_KEYS = ("transport_yen", "handling_yen", "coordination_yen", "system_yen",
             "existing_compensation_yen", "opportunity_cost_yen")


def safe_value(v: Any, depth: int = 0) -> None:
    """A small shared JSON domain, not a general-purpose JSON canonical standard."""
    if depth > 32:
        raise ValueError("JSONの入れ子が深すぎます")
    if v is None or type(v) is bool:
        return
    if type(v) is int:
        if abs(v) > MAX_INT:
            raise ValueError("整数の範囲外です")
        return
    if isinstance(v, str):
        if len(v) > 10000 or any(0xD800 <= ord(c) <= 0xDFFF for c in v):
            raise ValueError("文字列の範囲外です")
        return
    if isinstance(v, list):
        if len(v) > 20000:
            raise ValueError("配列が大きすぎます")
        for a in v:
            safe_value(a, depth + 1)
        return
    if isinstance(v, dict):
        if len(v) > 1000:
            raise ValueError("オブジェクトが大きすぎます")
        for k, a in v.items():
            if not isinstance(k, str) or not re.fullmatch(r"[\x20-\x7e]+", k) or k in {"__proto__", "constructor", "prototype"}:
                raise ValueError("不正なキーです")
            safe_value(k, depth + 1)
            safe_value(a, depth + 1)
        return
    raise ValueError("整数以外の数値や未対応の型は扱いません")


def canonical(v: Any) -> str:
    safe_value(v)
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(v: Any) -> str:
    return hashlib.sha256(canonical(v).encode("utf-8")).hexdigest()


def same(a: Any, b: Any) -> bool:
    return canonical(a) == canonical(b)


def parse_json(text: str) -> Any:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("JSONは2MB以内にしてください")
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in items:
            if k in out:
                raise ValueError("JSONキーが重複しています: " + k)
            out[k] = v
        return out
    def bad_number(s: str) -> Any:
        raise ValueError("整数以外のJSON数値は扱いません: " + s)
    obj = json.loads(text, object_pairs_hook=pairs, parse_float=bad_number, parse_constant=bad_number)
    safe_value(obj)
    return obj


def read_json(path: Path) -> Any:
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("JSONは2MB以内にしてください")
    return parse_json(raw.decode("utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
            tmp = f.name
            f.write(data)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)


def keys(obj: Any, wanted: list[str] | tuple[str, ...] | set[str], where: str) -> None:
    if not isinstance(obj, dict) or set(obj) != set(wanted):
        raise ValueError(where + ": 項目の不足・余分な項目があります")


def number(v: Any, where: str, low: int = 0, high: int = 1_000_000, nullable: bool = False) -> None:
    if v is None and nullable:
        return
    if type(v) is not int or not low <= v <= high:
        raise ValueError(where + ": 整数の範囲が不正です")


def flag(v: Any, where: str, nullable: bool = True) -> None:
    if type(v) is not bool and not (nullable and v is None):
        raise ValueError(where + ": true / false / nullで指定してください")


def identifier(v: Any, where: str) -> None:
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v):
        raise ValueError(where + ": 識別子が不正です")


def worst(values: list[str]) -> str:
    if any(v not in STATUSES for v in values):
        raise ValueError("未知の判定です")
    return "REJECT" if "REJECT" in values else "HOLD" if "HOLD" in values else "PASS"


def issue(status: str, code: str, party: str = "") -> dict[str, str]:
    return {"status": status, "code": code, "party": party}


def flag_issue(v: bool | None, unknown: str, denied: str, party: str = "") -> list[dict[str, str]]:
    return [issue("HOLD", unknown, party)] if v is None else [issue("REJECT", denied, party)] if not v else []


def binding(plan: dict[str, Any]) -> dict[str, Any]:
    return {"plan_id": plan["id"], "revision": plan["revision"], "plan_sha256": digest(plan)}


def participants(x: dict[str, Any]) -> list[str]:
    return ["CARRIER", "A", "B", x["plan"]["candidate"]]


def demo_refresh(x: dict[str, Any], what: str = "all") -> dict[str, Any]:
    """Explicit synthetic fixture action. Never invoked by evaluate() or verify()."""
    y = copy.deepcopy(x)
    b = binding(y["plan"])
    t = y["now"]
    if what in {"all", "planning", "approvals"}:
        y["approvals"] = [{**b, "party": p, "actor": "DEMO-" + p, "active": True,
                           "issued": max(0, t - 60), "expires": min(1440, t + 60)} for p in participants(y)]
    if what in {"all", "planning"}:
        y["evidence"] = {**b, "observed": max(0, t - 5), "expires": min(1440, t + 40),
                         "capacity_ok": True, "schedule_ok": True}
    if what in {"all", "handoff"}:
        y["handoff"] = {**b, "observed": t, "expires": min(1440, t + 30),
                        "sender": "CARRIER", "receiver": y["plan"]["candidate"],
                        "quantity_ok": True, "condition_ok": True,
                        "sender_ack": True, "receiver_ack": True}
    if what not in {"all", "planning", "approvals", "handoff"}:
        raise ValueError("未知の合成補完です")
    return y


def patch(x: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    y = copy.deepcopy(x)
    for path, value in changes.items():
        parts = path.split(".")
        node: Any = y
        for k in parts[:-1]:
            node = node[int(k)] if isinstance(node, list) else node[k]
        k = parts[-1]
        if isinstance(node, list):
            node[int(k)] = copy.deepcopy(value)
        else:
            if k not in node:
                raise ValueError("未知の変更対象: " + path)
            node[k] = copy.deepcopy(value)
    return y


class Engine:
    def __init__(self, fixtures: dict[str, Any]):
        safe_value(fixtures)
        if not isinstance(fixtures, dict) or fixtures.get("schema") != "joint-delivery-fixtures/v1":
            raise ValueError("ケース定義の形式が不正です")
        self.data = copy.deepcopy(fixtures)
        self.profile = self.data["profile"]
        self.profile_hash = digest(self.profile)
        if self.profile_hash != EXPECTED_PROFILE_SHA256:
            raise ValueError("固定した公開条件が変更されています。仕様変更として別途レビューが必要です")
        self.scenarios = self.data["scenarios"]
        ids = [s["id"] for s in self.scenarios]
        if len(ids) != len(set(ids)) or not ids:
            raise ValueError("ケースIDが重複または空です")

    def case(self, case_id: str) -> dict[str, Any]:
        s = next((s for s in self.scenarios if s["id"] == case_id), None)
        if s is None:
            raise ValueError("ケースがありません: " + case_id)
        x = demo_refresh(self.data["base"])
        x["case_id"] = s["id"]
        x = patch(x, s["changes"])
        if s["refresh"] != "none":
            x = demo_refresh(x, s["refresh"])
        self.validate(x)
        return x

    def validate(self, x: Any) -> None:
        safe_value(x)
        keys(x, ["schema", "case_id", "now", "synthetic", "mode", "plan", "business", "workload",
                 "approvals", "evidence", "handoff"], "入力")
        if x["schema"] != INPUT_SCHEMA:
            raise ValueError("入力形式が違います")
        identifier(x["case_id"], "ケースID")
        number(x["now"], "検証時刻", 0, 1439)
        flag(x["synthetic"], "合成データ区分", False)
        identifier(x["mode"], "利用範囲")
        p = x["plan"]
        keys(p, ["id", "revision", "policy_version", "route_id", "candidate", "shipments", "terms", "disclosure"], "計画")
        for k in ["id", "policy_version", "route_id", "candidate"]:
            identifier(p[k], "計画." + k)
        number(p["revision"], "計画版", 1, 1_000_000)
        if p["candidate"] not in self.profile["candidates"]:
            raise ValueError("追加候補は公開モデルのC社・D社だけです")
        keys(p["shipments"], ["A", "B", p["candidate"]], "貨物")
        for party, s in p["shipments"].items():
            keys(s, ["kg", "pallets", "arrival", "mixing"], "貨物." + party)
            number(s["kg"], "重量", 1, 1_000_000, True)
            number(s["pallets"], "パレット", 1, 1000, True)
            number(s["arrival"], "到着時刻", 0, 1439, True)
            flag(s["mixing"], "混載可否")
        terms = p["terms"]
        keys(terms, ["version", "agreements", "roles"], "合意条件")
        identifier(terms["version"], "合意条件版")
        keys(terms["agreements"], participants(x), "費用合意")
        for v in terms["agreements"].values():
            flag(v, "費用合意")
        keys(terms["roles"], ["pickup", "transport", "receipt"], "担当役割")
        for v in terms["roles"].values():
            identifier(v, "担当役割")
        d = p["disclosure"]
        keys(d, ["recipient", "fields"], "開示")
        identifier(d["recipient"], "開示先")
        if not isinstance(d["fields"], list) or not 1 <= len(d["fields"]) <= 20:
            raise ValueError("開示項目が不正です")
        for v in d["fields"]:
            identifier(v, "開示項目")
        if len(set(d["fields"])) != len(d["fields"]):
            raise ValueError("開示項目が重複しています")
        keys(x["business"], ["revenue_yen", *COST_KEYS, "initial_cost_yen"], "採算")
        for v in x["business"].values():
            number(v, "採算金額", 0, 100_000_000, True)
        keys(x["workload"], ["manual_minutes", "assisted_minutes", "setup_minutes"], "工数")
        for v in x["workload"].values():
            number(v, "工数", 0, 100_000, True)
        def bound(v: Any, extra: list[str], when: str) -> None:
            keys(v, ["plan_id", "revision", "plan_sha256", *extra], "根拠")
            identifier(v["plan_id"], "根拠の計画ID")
            number(v["revision"], "根拠の計画版", 1, 1_000_000)
            if not isinstance(v["plan_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", v["plan_sha256"]):
                raise ValueError("根拠のハッシュ形式が不正です")
            number(v[when], "根拠の開始時刻", 0, 1439)
            number(v["expires"], "根拠の終了時刻", 1, 1440)
            if v[when] >= v["expires"]:
                raise ValueError("根拠の有効期間が不正です")
        a = x["approvals"]
        if not isinstance(a, list) or len(a) > 8:
            raise ValueError("承認リストが不正です")
        for v in a:
            if v is None:
                continue
            bound(v, ["party", "actor", "active", "issued", "expires"], "issued")
            identifier(v["party"], "承認主体")
            identifier(v["actor"], "合成承認者")
            flag(v["active"], "承認状態", False)
        if x["evidence"] is not None:
            v = x["evidence"]
            bound(v, ["observed", "expires", "capacity_ok", "schedule_ok"], "observed")
            flag(v["capacity_ok"], "容量確認")
            flag(v["schedule_ok"], "計画確認")
        if x["handoff"] is not None:
            v = x["handoff"]
            bound(v, ["observed", "expires", "sender", "receiver", "quantity_ok", "condition_ok", "sender_ack", "receiver_ack"], "observed")
            identifier(v["sender"], "送り手")
            identifier(v["receiver"], "受け手")
            for k in ["quantity_ok", "condition_ok", "sender_ack", "receiver_ack"]:
                flag(v[k], k)

    def bound_issues(self, x: dict[str, Any], v: dict[str, Any], when: str, party: str = "") -> list[dict[str, str]]:
        p, out = x["plan"], []
        if v["plan_id"] != p["id"]:
            out.append(issue("REJECT", "BINDING_TARGET", party))
        if v["revision"] < p["revision"]:
            out.append(issue("HOLD", "BINDING_OLD", party))
        elif v["revision"] > p["revision"]:
            out.append(issue("REJECT", "BINDING_FUTURE", party))
        elif v["plan_sha256"] != digest(p):
            out.append(issue("REJECT", "BINDING_HASH", party))
        if x["now"] < v[when]:
            out.append(issue("HOLD", "NOT_YET_VALID", party))
        if x["now"] >= v["expires"]:
            out.append(issue("HOLD", "EXPIRED", party))
        return out

    def evaluate(self, x: dict[str, Any]) -> dict[str, Any]:
        self.validate(x)
        p, prof = x["plan"], self.profile
        s, candidate = p["shipments"], p["candidate"]
        checks: list[dict[str, Any]] = []
        def check(i: int, items: list[dict[str, str]]) -> None:
            checks.append({"id": f"R{i:02d}", "status": worst([v["status"] for v in items]), "issues": items})
        z: list[dict[str, str]] = []
        if p["route_id"] != prof["route_id"] or p["policy_version"] != prof["policy_version"]:
            z.append(issue("REJECT", "TARGET_MISMATCH"))
        for party in ["A", "B"]:
            for k in ["kg", "pallets"]:
                if s[party][k] is None:
                    z.append(issue("HOLD", "MISSING_EXISTING_CARGO", party))
                elif s[party][k] != prof["existing"][party][k]:
                    z.append(issue("REJECT", "EXISTING_CARGO_CHANGED", party))
        check(1, z)
        z = []
        for k, cap, over, unknown in [("kg", prof["capacity_kg"], "WEIGHT_OVER", "WEIGHT_UNKNOWN"),
                                      ("pallets", prof["capacity_pallets"], "PALLETS_OVER", "PALLETS_UNKNOWN")]:
            vals = [s[party][k] for party in ["A", "B", candidate]]
            if sum(v for v in vals if v is not None) > cap:
                z.append(issue("REJECT", over))
            if any(v is None for v in vals):
                z.append(issue("HOLD", unknown))
        for party in ["A", "B", candidate]:
            z += flag_issue(s[party]["mixing"], "MIXING_UNKNOWN", "MIXING_DENIED", party)
        check(2, z)
        z = []
        for party in ["A", "B", candidate]:
            c = prof["existing"].get(party) or prof["candidates"][party]
            arrival = s[party]["arrival"]
            if arrival is None:
                z.append(issue("HOLD", "ARRIVAL_UNKNOWN", party))
            else:
                if not c["window_start"] <= arrival <= c["window_end"]:
                    z.append(issue("REJECT", "WINDOW_VIOLATION", party))
                if party in prof["existing"] and arrival > c["baseline_arrival"] + c["max_delay"]:
                    z.append(issue("REJECT", "EXISTING_DELAY", party))
        for a, b in [("A", candidate), (candidate, "B")]:
            if s[a]["arrival"] is not None and s[b]["arrival"] is not None and s[a]["arrival"] > s[b]["arrival"]:
                z.append(issue("REJECT", "STOP_ORDER"))
        check(3, z)
        z = []
        t = p["terms"]
        if t["version"] != prof["terms_version"]:
            z.append(issue("REJECT", "TERMS_VERSION"))
        for party in participants(x):
            z += flag_issue(t["agreements"][party], "TERMS_UNKNOWN", "TERMS_DENIED", party)
        if t["roles"] != {"pickup": "CARRIER", "transport": "CARRIER", "receipt": candidate}:
            z.append(issue("REJECT", "ROLE_MISMATCH"))
        check(4, z)
        d = p["disclosure"]
        check(5, [] if d["recipient"] == "participants" and all(v in prof["disclosure_fields"] for v in d["fields"])
              else [issue("REJECT", "DISCLOSURE_DENIED")])
        z = []
        actual = [a for a in x["approvals"] if a is not None]
        ids = [a["party"] for a in actual]
        if len(ids) != len(set(ids)):
            z.append(issue("REJECT", "APPROVAL_DUPLICATE"))
        if any(v not in participants(x) for v in ids):
            z.append(issue("REJECT", "APPROVAL_EXTRA"))
        for party in participants(x):
            found = [a for a in actual if a["party"] == party]
            if not found:
                z.append(issue("HOLD", "APPROVAL_MISSING", party))
            for a in found:
                z += self.bound_issues(x, a, "issued", party)
                if a["actor"] != "DEMO-" + party:
                    z.append(issue("REJECT", "ACTOR_MISMATCH", party))
                if not a["active"]:
                    z.append(issue("HOLD", "REVOKED", party))
        check(6, z)
        e = x["evidence"]
        z = [issue("HOLD", "EVIDENCE_MISSING")] if e is None else self.bound_issues(x, e, "observed")
        if e is not None:
            z += flag_issue(e["capacity_ok"], "CAPACITY_UNCONFIRMED", "CAPACITY_DENIED")
            z += flag_issue(e["schedule_ok"], "SCHEDULE_UNCONFIRMED", "SCHEDULE_DENIED")
        check(7, z)
        check(8, [] if x["synthetic"] and x["mode"] == "simulation" else [issue("REJECT", "SCOPE_DENIED")])
        status = worst([c["status"] for c in checks])
        h = x["handoff"]
        hz = [issue("HOLD", "HANDOFF_MISSING")] if h is None else self.bound_issues(x, h, "observed")
        if h is not None:
            if h["sender"] != "CARRIER" or h["receiver"] != candidate:
                hz.append(issue("REJECT", "HANDOFF_PARTIES"))
            for key, unknown, denied in [("quantity_ok", "QUANTITY_UNKNOWN", "QUANTITY_MISMATCH"),
                                         ("condition_ok", "CONDITION_UNKNOWN", "CONDITION_BAD"),
                                         ("sender_ack", "SENDER_UNKNOWN", "SENDER_DENIED"),
                                         ("receiver_ack", "RECEIVER_UNKNOWN", "RECEIVER_DENIED")]:
                hz += flag_issue(h[key], unknown, denied)
        if status != "PASS":
            hz.insert(0, issue(status, "PARTICIPATION_BLOCKED"))
        handoff_status = worst([v["status"] for v in hz])
        b, w = self.business(x["business"]), self.workload(x["workload"])
        return {"participation": status, "checks": checks,
                "handoff": {"status": handoff_status, "issues": hz},
                "business": b, "workload": w,
                "simulation_ready": status == "PASS" and handoff_status == "PASS"}

    @staticmethod
    def business(b: dict[str, Any]) -> dict[str, Any]:
        costs = [b[k] for k in COST_KEYS]
        cost = None if any(v is None for v in costs) else sum(costs)
        net = None if cost is None or b["revenue_yen"] is None else b["revenue_yen"] - cost
        status = "UNKNOWN" if net is None else "POSITIVE" if net > 0 else "NEGATIVE" if net < 0 else "BREAKEVEN"
        initial = b["initial_cost_yen"]
        trips = None if net is None or net <= 0 or initial is None else (initial + net - 1) // net
        return {"status": status, "cost_yen": cost, "contribution_yen": net, "break_even_trips": trips,
                "basis": "synthetic_per_trip_estimate"}

    @staticmethod
    def workload(w: dict[str, Any]) -> dict[str, Any]:
        saved = None if w["manual_minutes"] is None or w["assisted_minutes"] is None else w["manual_minutes"] - w["assisted_minutes"]
        count = None if saved is None or saved <= 0 or w["setup_minutes"] is None else (w["setup_minutes"] + saved - 1) // saved
        return {"saved_minutes": saved, "setup_break_even_cases": count, "basis": "synthetic_assumption_not_measurement"}

    def record(self, x: dict[str, Any]) -> dict[str, Any]:
        body = {"schema": RECORD_SCHEMA, "profile_sha256": self.profile_hash,
                "input": copy.deepcopy(x), "result": self.evaluate(x)}
        return {**body, "record_sha256": digest(body)}

    def verify_record(self, record: Any) -> dict[str, Any]:
        keys(record, ["schema", "profile_sha256", "input", "result", "record_sha256"], "検証記録")
        if record["schema"] != RECORD_SCHEMA or record["profile_sha256"] != self.profile_hash:
            raise ValueError("記録の形式・固定条件が一致しません")
        expected = self.record(record["input"])
        if not same(record, expected):
            raise ValueError("記録・判定・根拠の再計算が一致しません")
        return {"verified": True, "participation": record["result"]["participation"],
                "handoff": record["result"]["handoff"]["status"]}

    def summary(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        return {"records": len(records),
                "participation": {s: sum(r["result"]["participation"] == s for r in records) for s in STATUSES},
                "handoff": {s: sum(r["result"]["handoff"]["status"] == s for r in records) for s in STATUSES},
                "simulation_ready": sum(r["result"]["simulation_ready"] for r in records)}

    def suite(self) -> dict[str, Any]:
        records = [self.record(self.case(s["id"])) for s in self.scenarios]
        body = {"schema": BUNDLE_SCHEMA, "suite": "standard", "profile_sha256": self.profile_hash,
                "records": records, "summary": self.summary(records)}
        return {**body, "bundle_sha256": digest(body)}

    def verify(self, obj: Any) -> dict[str, Any]:
        if not isinstance(obj, dict):
            raise ValueError("検証記録ではありません")
        if obj.get("schema") == RECORD_SCHEMA:
            return self.verify_record(obj)
        keys(obj, ["schema", "suite", "profile_sha256", "records", "summary", "bundle_sha256"], "ケース一式")
        if obj["schema"] != BUNDLE_SCHEMA or obj["suite"] != "standard" or obj["profile_sha256"] != self.profile_hash:
            raise ValueError("ケース一式の形式・条件が一致しません")
        if not isinstance(obj["records"], list) or len(obj["records"]) != len(self.scenarios):
            raise ValueError("標準ケースが不足または過剰です")
        for s, r in zip(self.scenarios, obj["records"]):
            self.verify_record(r)
            if not same(r["input"], self.case(s["id"])):
                raise ValueError("標準ケースの入力・順序が変更されています")
        body = {k: v for k, v in obj.items() if k != "bundle_sha256"}
        if not same(obj["summary"], self.summary(obj["records"])) or obj["bundle_sha256"] != digest(body):
            raise ValueError("集計・ケース一式の内容が一致しません")
        return {"verified": True, **obj["summary"]}

    def permit(self, record: dict[str, Any]) -> dict[str, Any]:
        self.verify_record(record)
        if not record["result"]["simulation_ready"]:
            raise ValueError("参加・引渡し条件が成立していません")
        r = record["record_sha256"]
        return {"schema": PERMIT_SCHEMA, "record_sha256": r,
                "plan_sha256": digest(record["input"]["plan"]), "nonce": digest(["simulation", record["input"]["plan"]["route_id"], record["input"]["plan"]["id"], record["input"]["plan"]["revision"], record["input"]["plan"]["candidate"]])}

    def gate(self, record: Any, permit: Any, current: Any, used: Any) -> dict[str, Any]:
        """Simulation only. A caller must retain state; no production execution occurs."""
        if not isinstance(used, list) or len(used) > 10000 or any(not isinstance(n, str) or not re.fullmatch(r"[0-9a-f]{64}", n) for n in used) or len(used) != len(set(used)):
            raise ValueError("模擬使用状態が不正です")
        def deny(reason: str) -> dict[str, Any]:
            return {"allowed": False, "reason": reason, "used": copy.deepcopy(used), "operation": None}
        try:
            self.verify_record(record)
            self.validate(current)
            if not same(permit, self.permit(record)):
                return deny("PERMIT_MISMATCH")
            if permit["plan_sha256"] != digest(current["plan"]):
                return deny("PLAN_CHANGED")
            if current["now"] < record["input"]["now"]:
                return deny("CLOCK_ROLLBACK")
            if not self.evaluate(current)["simulation_ready"]:
                return deny("CURRENT_CONDITIONS_BLOCKED")
            if permit["nonce"] in used:
                return deny("ALREADY_USED")
        except (ValueError, TypeError, KeyError):
            return deny("INVALID_EVIDENCE")
        return {"allowed": True, "reason": "SIMULATED_HANDOFF", "used": used + [permit["nonce"]],
                "operation": {"kind": "simulated_handoff", "route_id": current["plan"]["route_id"],
                              "candidate": current["plan"]["candidate"], "revision": current["plan"]["revision"]}}

    def partner_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        self.verify_record(record)
        if record["result"]["checks"][4]["status"] != "PASS":
            raise ValueError("開示条件が成立していません")
        p, r = record["input"]["plan"], record["result"]
        fields = {"route_id": p["route_id"], "candidate": p["candidate"], "revision": p["revision"],
                  "participation": r["participation"], "handoff": r["handoff"]["status"],
                  "checks": [{"id": c["id"], "status": c["status"]} for c in r["checks"]]}
        return {"schema": "joint-delivery-partner-summary/v1", "synthetic": True,
                "fields": {k: fields[k] for k in p["disclosure"]["fields"]}}


def load_engine() -> Engine:
    return Engine(read_json(ROOT / "cases.json"))


def samples(engine: Engine) -> list[dict[str, Any]]:
    out = [engine.case(s["id"]) for s in engine.scenarios]
    base = engine.case("JD01")
    for kg, pallets, arrival in itertools.product([600, 1200, 1201, None], [2, 4, 5], [539, 570, 600, 601, None]):
        x = patch(base, {"plan.shipments.C.kg": kg, "plan.shipments.C.pallets": pallets, "plan.shipments.C.arrival": arrival})
        out.append(demo_refresh(x))
    for field in ["capacity_ok", "schedule_ok"]:
        for v in [None, False, True]:
            out.append(patch(base, {"evidence." + field: v}))
    for field in ["quantity_ok", "condition_ok", "sender_ack", "receiver_ack"]:
        for v in [None, False, True]:
            out.append(patch(base, {"handoff." + field: v}))
    for now in [509, 510, 564, 565, 569, 570, 599, 600, 609, 610, 629, 630]:
        out.append(patch(base, {"now": now}))
    for amount in [0, 11999, 12000, 12001, 20000, None]:
        out.append(patch(base, {"business.revenue_yen": amount}))
    return out


def self_test(engine: Engine) -> dict[str, int]:
    counts: dict[str, int] = {"scenario_assertions": 0, "boundary_records": 0, "tamper_checks": 0, "input_checks": 0, "gate_checks": 0, "disclosure_checks": 0, "aggregation_vectors": 0}
    def ok(condition: bool, category: str, label: str) -> None:
        if not condition:
            raise AssertionError(label)
        counts[category] += 1
    def fails(fn: Any, category: str, label: str) -> None:
        try:
            fn()
        except (ValueError, TypeError, KeyError):
            ok(True, category, label)
        else:
            raise AssertionError("拒否されませんでした: " + label)
    for s in engine.scenarios:
        r = engine.record(engine.case(s["id"]))
        got = {"participation": r["result"]["participation"], "handoff": r["result"]["handoff"]["status"], "business": r["result"]["business"]["status"]}
        ok(same(got, s["expected"]), "scenario_assertions", s["id"] + repr(got))
        ok(engine.verify_record(r)["verified"], "scenario_assertions", "record " + s["id"])
    for x in samples(engine):
        r = engine.record(x)
        ok(engine.verify_record(r)["verified"], "boundary_records", "boundary record")
    base = engine.case("JD01")
    rec = engine.record(base)
    for path, value in {"result.participation": "HOLD", "result.simulation_ready": False,
                        "result.business.contribution_yen": 999999, "input.business.revenue_yen": 999999,
                        "input.plan.shipments.C.kg": 800, "input.now": 571,
                        "profile_sha256": "0" * 64, "record_sha256": "0" * 64}.items():
        bad = patch(rec, {path: value})
        fails(lambda bad=bad: engine.verify_record(bad), "tamper_checks", path)
        bad["record_sha256"] = digest({k: v for k, v in bad.items() if k != "record_sha256"})
        if path in {"record_sha256", "input.now"}:
            ok(engine.verify_record(bad)["verified"], "tamper_checks", "unchanged payload restored")
        else:
            fails(lambda bad=bad: engine.verify_record(bad), "tamper_checks", "rehashed " + path)
    suite = engine.suite()
    ok(engine.verify(suite)["verified"], "tamper_checks", "suite")
    bad = copy.deepcopy(suite)
    bad["records"].pop()
    bad["summary"] = engine.summary(bad["records"])
    bad["bundle_sha256"] = digest({k: v for k, v in bad.items() if k != "bundle_sha256"})
    fails(lambda: engine.verify(bad), "tamper_checks", "deleted case and rebuilt digest")
    for text in ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1.0}', '{"a":1e2}', '{"a":9007199254740992}', '{"__proto__":{}}', '{"a":"\\ud800"}', '{"a":1} trailing']:
        fails(lambda text=text: parse_json(text), "input_checks", text)
    for field, vals in [("plan.shipments.C.kg", [True, -1, 0, 1.5, "600"]),
                        ("plan.shipments.C.arrival", [False, -1, 1440, "570"]),
                        ("plan.shipments.C.mixing", [1, "true", []]),
                        ("business.revenue_yen", [True, -1, 1.5, "20000"]),
                        ("now", [None, True, -1, 1440]),
                        ("plan.revision", [0, True, "1"]),
                        ("plan.candidate", ["A", "E", "__proto__"]),
                        ("approvals", [{}, [False]]),
                        ("plan.disclosure.fields", [[], ["checks", "checks"]])]:
        for val in vals:
            bad = patch(base, {field: val})
            fails(lambda bad=bad: engine.evaluate(bad), "input_checks", field + repr(val))
    bad = copy.deepcopy(base); bad["unexpected"] = 1
    fails(lambda: engine.evaluate(bad), "input_checks", "unknown key")
    for party in ["A", "B", "C"]:
        bad = copy.deepcopy(base); del bad["plan"]["shipments"][party]
        fails(lambda bad=bad: engine.evaluate(bad), "input_checks", "missing shipment")
    permit = engine.permit(rec)
    g = engine.gate(rec, permit, base, [])
    ok(g["allowed"] and len(g["used"]) == 1, "gate_checks", "initial gate")
    gg = engine.gate(rec, permit, base, g["used"])
    ok(not gg["allowed"] and gg["used"] == g["used"], "gate_checks", "replay")
    for x in [patch(base, {"approvals.3.active": False}), patch(base, {"now": 630}),
              patch(base, {"now": 569}), patch(base, {"handoff.receiver_ack": None}),
              demo_refresh(patch(base, {"plan.revision": 2})), engine.case("JD14")]:
        gg = engine.gate(rec, permit, x, [])
        ok(not gg["allowed"] and gg["used"] == [] and gg["operation"] is None, "gate_checks", "current gate")
    for s in engine.scenarios:
        r = engine.record(engine.case(s["id"]))
        if not r["result"]["simulation_ready"]:
            fails(lambda r=r: engine.permit(r), "gate_checks", "nonpass permit")
    loss = engine.record(engine.case("JD32"))
    ok(loss["result"]["participation"] == "PASS" and loss["result"]["business"]["status"] == "NEGATIVE", "gate_checks", "economic separation")
    public = engine.partner_summary(rec)
    ok(set(public["fields"]) == set(engine.profile["disclosure_fields"]), "disclosure_checks", "allowlist")
    ok(not any(k in canonical(public) for k in ["revenue_yen", "plan_sha256", "approvals", "kg", "contribution_yen"]), "disclosure_checks", "no internal fields")
    fails(lambda: engine.partner_summary(engine.record(engine.case("JD21"))), "disclosure_checks", "forbidden request")
    for vector in itertools.product(STATUSES, repeat=8):
        got = worst(list(vector))
        expected = STATUSES[max(STATUSES.index(v) for v in vector)]
        ok(got == expected, "aggregation_vectors", "aggregation")
    return counts


def check_embedded(engine: Engine, write: bool = False) -> None:
    path = ROOT / "index.html"
    html = path.read_text(encoding="utf-8")
    pattern = r'(<script id="fixture-data" type="application/json">)(.*?)(</script>)'
    match = re.search(pattern, html, re.S)
    if not match:
        raise ValueError("HTMLのデータ領域がありません")
    if write:
        payload = canonical(engine.data).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        html = html[:match.start(2)] + payload + html[match.end(2):]
        path.write_text(html, encoding="utf-8")
    elif not same(parse_json(match.group(2)), engine.data):
        raise ValueError("cases.jsonとHTMLのデータが一致しません。python poc.py sync が必要です")
    if EXPECTED_PROFILE_SHA256 not in html:
        raise ValueError("HTMLの固定参照値が一致しません")


def javascript_test(engine: Engine) -> dict[str, int]:
    check_embedded(engine)
    if not shutil.which("node"):
        raise RuntimeError("Python/JavaScript照合にはNode.jsが必要です")
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    source = re.search(r'<script id="jd-engine">(.*?)</script>', html, re.S)
    if not source:
        raise ValueError("JavaScript検証器がありません")
    xs = samples(engine)
    script = source.group(1) + r'''
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const e = new JD.Engine(input.fixtures);
const output = input.inputs.map(x => e.record(x));
const r = e.record(input.inputs[0]);
const p = e.permit(r); const g = e.gate(r,p,r.input,[]);
const bytes = ['','abc','共同配送','😀'];
const crypto = require('node:crypto');
if (!bytes.every(s => JD.sha256(s) === crypto.createHash('sha256').update(s).digest('hex'))) throw Error('SHA256 mismatch');
const badJSON = ['{"a":1,"a":2}','{"a":NaN}','{"a":1.0}','{"a":1e2}','{"__proto__":{}}','{"a":"\\ud800"}'];
for (const s of badJSON) { let denied=false; try { JD.parseJSON(s); } catch (_) {denied=true;} if(!denied) throw Error('strict JSON '+s); }
process.stdout.write(JSON.stringify({records:output, gate:g, replay:e.gate(r,p,r.input,g.used), summary:e.partnerSummary(r), suite:e.suite(), hashChecks:bytes.length, parserChecks:badJSON.length}));
'''
    proc = subprocess.run(["node", "-e", script], input=json.dumps({"fixtures": engine.data, "inputs": xs}, ensure_ascii=False),
                          capture_output=True, text=True, encoding="utf-8", timeout=120, check=True)
    data = parse_json(proc.stdout)
    if len(data["records"]) != len(xs):
        raise AssertionError("JavaScript件数が一致しません")
    for i, (x, r) in enumerate(zip(xs, data["records"])):
        engine.verify_record(r)
        if not same(engine.record(x), r):
            raise AssertionError("Python/JS不一致: " + str(i))
    r = engine.record(xs[0]); p = engine.permit(r); g = engine.gate(r, p, xs[0], [])
    for a, b in [(data["gate"], g), (data["replay"], engine.gate(r, p, xs[0], g["used"])),
                 (data["summary"], engine.partner_summary(r)), (data["suite"], engine.suite())]:
        if not same(a, b):
            raise AssertionError("Python/JSの操作・出力が一致しません")
    return {"cross_language_records": len(xs), "cross_language_outputs": 4,
            "sha256_vectors": data["hashChecks"], "strict_json_cases": data["parserChecks"]}


def main() -> int:
    p = argparse.ArgumentParser(description="共同配送・参加拡張アシュアランス（合成データ・ローカルPoC）")
    sub = p.add_subparsers(dest="command")
    run = sub.add_parser("run", help="標準ケースを1つのJSONへ保存")
    run.add_argument("--case", dest="case_id")
    run.add_argument("--out", type=Path, default=ROOT / "output" / "results.json")
    v = sub.add_parser("verify", help="Python・ブラウザで保存した記録を再検証")
    v.add_argument("path", type=Path)
    ev = sub.add_parser("evaluate", help="公開モデルの入力JSONを検証")
    ev.add_argument("path", type=Path); ev.add_argument("--out", type=Path, default=ROOT / "output" / "results.json")
    ex = sub.add_parser("export-input", help="合成ケースの完全な入力を取り出す")
    ex.add_argument("case_id", nargs="?", default="JD01"); ex.add_argument("--out", type=Path, default=ROOT / "output" / "input.json")
    sub.add_parser("test", help="Python自己テスト")
    sub.add_parser("ci", help="Python・JavaScript・ファイル整合性検査。Leanは別コマンド")
    sub.add_parser("sync", help="ケース定義を自己完結HTMLへ同期")
    sub.add_parser("lean-test", help="Leanの証明・入力からの対応を検査")
    bt = sub.add_parser("browser-test", help="Playwrightでfile://とHTTPの操作を検査")
    bt.add_argument("--browser", help="Chromium実行ファイル（省略時はPlaywright管理版）")
    bt.add_argument("--entry", choices=["file", "content"], default="file", help="通常はfile。contentはHTML直接読込だけを検査")
    args = p.parse_args()
    try:
        e = load_engine()
        command = args.command or "run"
        if command == "run":
            out = getattr(args, "out", ROOT / "output" / "results.json")
            case_id = getattr(args, "case_id", None)
            obj = e.record(e.case(case_id)) if case_id else e.suite()
            write_json(out, obj); print(json.dumps(e.verify(obj), ensure_ascii=False)); print(str(out.resolve()))
        elif command == "verify":
            try:
                print(json.dumps(e.verify(read_json(args.path)), ensure_ascii=False))
            except (ValueError, TypeError, KeyError) as err:
                print("再検証不成立: " + str(err), file=sys.stderr); return 1
        elif command == "evaluate":
            obj = e.record(read_json(args.path)); write_json(args.out, obj)
            print(json.dumps(e.verify_record(obj), ensure_ascii=False)); print(str(args.out.resolve()))
        elif command == "export-input":
            write_json(args.out, e.case(args.case_id)); print(str(args.out.resolve()))
        elif command == "test":
            print(json.dumps(self_test(e), ensure_ascii=False))
        elif command == "ci":
            result = {"python": self_test(e), "javascript": javascript_test(e)}
            audit_sources()
            print(json.dumps(result, ensure_ascii=False))
        elif command == "sync":
            check_embedded(e, True); print("cases.json と index.html を同期しました")
        elif command == "lean-test":
            print(json.dumps(lean_test(e), ensure_ascii=False))
        elif command == "browser-test":
            print(json.dumps(browser_test(e, args.browser, args.entry), ensure_ascii=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as err:
        print("入力・環境エラー: " + str(err), file=sys.stderr)
        if isinstance(err, subprocess.CalledProcessError):
            print((err.stdout or "") + (err.stderr or ""), file=sys.stderr)
        return 2
    except AssertionError as err:
        print("検査不一致: " + str(err), file=sys.stderr); return 1

# The source audit, Lean bridge, and browser tests follow in this same file.

def audit_sources() -> dict[str, int]:
    lean = (ROOT / "JointDeliveryAssurance.lean").read_text(encoding="utf-8")
    # Strip ordinary comments before checking proof declarations; no nested block comments used.
    clean = re.sub(r"/-.*?-/", "", lean, flags=re.S)
    clean = re.sub(r"--[^\n]*", "", clean)
    if re.search(r"\b(?:sorry|admit|axiom|native_decide|implemented_by|unsafe)\b", clean):
        raise AssertionError("Leanに未証明宣言・独自公理等があります")
    if re.findall(r"(?m)^import\s+(.+)$", clean) != ["Std"]:
        raise AssertionError("Lean依存関係のレビューが必要です")
    if EXPECTED_PROFILE_SHA256 not in lean:
        raise AssertionError("Leanの参照条件が一致しません")
    names = re.findall(r"(?m)^theorem\s+([A-Za-z_][A-Za-z_0-9]*)", clean)
    if len(names) != len(set(names)) or len(names) < 30:
        raise AssertionError("Lean定理の構成が不正です")
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    if re.search(r'<script\b[^>]*\bsrc\s*=|<link\b[^>]*\bhref\s*=\s*["\']https?://', html, re.I):
        raise AssertionError("HTMLに外部依存があります")
    return {"theorem_declarations": len(names)}


def lean_projection(engine: Engine, x: dict[str, Any]) -> str:
    """Project inputs, not evaluate() results. Hash and identifier equality are boundaries."""
    engine.validate(x)
    p, prof = x["plan"], engine.profile
    def boolean(v: bool) -> str:
        if type(v) is not bool:
            raise TypeError("Lean観測値はBoolである必要があります")
        return "true" if v else "false"
    def opt(v: Any) -> str:
        if v is None:
            return "none"
        return "(some " + (boolean(v) if type(v) is bool else str(v)) + ")"
    def struct(d: dict[str, str]) -> str:
        return "{ " + ", ".join(k + " := " + v for k, v in d.items()) + " }"
    def ls(items: Any) -> str:
        return "[" + ", ".join(items) + "]"
    def bound(v: dict[str, Any]) -> str:
        return struct({"targetMatches": boolean(v["plan_id"] == p["id"]),
                       "revision": str(v["revision"]), "expectedRevision": str(p["revision"]),
                       "hashMatches": boolean(v["plan_sha256"] == digest(p))})
    def approval(v: Any, party: str) -> str:
        if v is None:
            return "none"
        return "(some " + struct({"binding": bound(v), "actorMatches": boolean(v["actor"] == "DEMO-" + party),
                                  "active": boolean(v["active"]), "issued": str(v["issued"]), "expires": str(v["expires"])}) + ")"
    actual = [a for a in x["approvals"] if a is not None]
    ids = [a["party"] for a in actual]
    approvals = []
    for party in participants(x):
        found = [a for a in actual if a["party"] == party]
        approvals += [approval(a, party) for a in found] if found else ["none"]
    order = ["A", "B", p["candidate"]]
    deliveries = []
    for party in order:
        c = prof["existing"].get(party) or prof["candidates"][party]
        deliveries.append(struct({"arrival": opt(p["shipments"][party]["arrival"]),
                                  "windowStart": str(c["window_start"]), "windowEnd": str(c["window_end"]),
                                  "latestExisting": opt(c["baseline_arrival"] + c["max_delay"] if party in prof["existing"] else None)}))
    ev = x["evidence"]
    evidence = "none" if ev is None else "(some " + struct({"binding": bound(ev), "observed": str(ev["observed"]),
        "expires": str(ev["expires"]), "capacity": opt(ev["capacity_ok"]), "schedule": opt(ev["schedule_ok"])}) + ")"
    h = x["handoff"]
    handoff = "none" if h is None else "(some " + struct({"binding": bound(h), "observed": str(h["observed"]),
        "expires": str(h["expires"]), "partiesMatch": boolean(h["sender"] == "CARRIER" and h["receiver"] == p["candidate"]),
        "quantity": opt(h["quantity_ok"]), "condition": opt(h["condition_ok"]),
        "sender": opt(h["sender_ack"]), "receiver": opt(h["receiver_ack"])}) + ")"
    return struct({
        "now": str(x["now"]), "targetMatches": boolean(p["route_id"] == prof["route_id"] and p["policy_version"] == prof["policy_version"]),
        "existingCargo": ls("(" + opt(p["shipments"][party][k]) + ", " + str(prof["existing"][party][k]) + ")" for party in ["A", "B"] for k in ["kg", "pallets"]),
        "weights": ls(opt(p["shipments"][party]["kg"]) for party in order),
        "pallets": ls(opt(p["shipments"][party]["pallets"]) for party in order),
        "mixing": ls(opt(p["shipments"][party]["mixing"]) for party in order),
        "deliveries": ls(deliveries),
        "stopOrder": ls("(" + opt(p["shipments"][a]["arrival"]) + ", " + opt(p["shipments"][b]["arrival"]) + ")" for a, b in [("A", p["candidate"]), (p["candidate"], "B")]),
        "termsMatch": boolean(p["terms"]["version"] == prof["terms_version"]),
        "agreements": ls(opt(p["terms"]["agreements"][party]) for party in participants(x)),
        "rolesMatch": boolean(p["terms"]["roles"] == {"pickup": "CARRIER", "transport": "CARRIER", "receipt": p["candidate"]}),
        "disclosureAllowed": boolean(p["disclosure"]["recipient"] == "participants" and all(f in prof["disclosure_fields"] for f in p["disclosure"]["fields"])),
        "approvalShape": boolean(len(ids) == len(set(ids)) and all(party in participants(x) for party in ids)),
        "approvals": ls(approvals), "evidence": evidence,
        "scopeAllowed": boolean(x["synthetic"] and x["mode"] == "simulation"), "handoff": handoff})


def lean_test(engine: Engine) -> dict[str, int]:
    audit = audit_sources()
    if not shutil.which("lake"):
        raise RuntimeError("Lean/Lakeがありません。READMEの導入手順に従ってください。未実行を証明成功とは扱いません")
    subprocess.run(["lake", "build"], cwd=ROOT, check=True, timeout=240)
    source = (ROOT / "JointDeliveryAssurance.lean").read_text(encoding="utf-8")
    names = re.findall(r"(?m)^theorem\s+([A-Za-z_][A-Za-z_0-9]*)", source)
    xs = samples(engine)
    with tempfile.TemporaryDirectory(prefix="joint-delivery-lean-") as td:
        for offset in range(0, len(xs), 32):
            lines = ["import JointDeliveryAssurance", "open JointDeliveryAssurance",
                     "set_option maxRecDepth 100000", "set_option maxHeartbeats 0"]
            for i, x in enumerate(xs[offset:offset + 32], offset):
                r = engine.evaluate(x)
                lines.append(f"def sample{i} : Observation := " + lean_projection(engine, x))
                expected = "[" + ", ".join("Status." + c["status"] for c in r["checks"]) + "]"
                lines.append(f"example : checks sample{i} = {expected} := by decide")
                lines.append(f"example : participation sample{i} = Status.{r['participation']} := by decide")
                lines.append(f"example : handoff sample{i} = Status.{r['handoff']['status']} := by decide")
                revenue = "none" if x["business"]["revenue_yen"] is None else f"(some {x['business']['revenue_yen']})"
                costs = ", ".join("none" if x["business"][k] is None else f"(some {x['business'][k]})" for k in COST_KEYS)
                lines.append(f"def money{i} : Economics := {{ revenue := {revenue}, costs := [{costs}] }}")
                lines.append(f"example : business money{i} = BusinessStatus.{r['business']['status']} := by decide")
            path = Path(td) / f"Inputs{offset}.lean"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            subprocess.run(["lake", "env", "lean", str(path)], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8", timeout=120)
        path = Path(td) / "Dependencies.lean"
        path.write_text("import JointDeliveryAssurance\n" + "\n".join("#print axioms JointDeliveryAssurance." + n for n in names) + "\n", encoding="utf-8")
        proc = subprocess.run(["lake", "env", "lean", str(path)], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8", timeout=120)
        reported = re.findall(r"depends on axioms:\s*\[([^\]]*)\]", proc.stdout, re.S)
        allowed = {"propext", "Classical.choice", "Quot.sound"}
        for report in reported:
            if set(v.strip() for v in report.split(",") if v.strip()) - allowed:
                raise AssertionError("想定外の証明依存があります: " + report)
        if "sorryAx" in proc.stdout or "Lean.ofReduceBool" in proc.stdout:
            raise AssertionError("想定外の証明依存があります")
    return {**audit, "projected_inputs": len(xs), "concrete_equalities": 4 * len(xs), "audited_theorems": len(names)}


def browser_test(engine: Engine, executable: str | None = None, entry: str = "file") -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright, expect as pw_expect
    except ImportError as err:
        raise RuntimeError("ブラウザ検査にはPlaywrightとChromiumが必要です。READMEを参照してください") from err
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import threading
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return None
    counts = {"scenario_views": 0, "interaction_assertions": 0, "file_checks": 0, "viewports": 0, "entrypoints": 0, "external_requests": 0, "public_view_assertions": 0, "public_control_assertions": 0}
    def expect(ok: bool, category: str, message: str) -> None:
        if not ok:
            raise AssertionError(message)
        counts[category] += 1
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="joint-delivery-browser-") as td, sync_playwright() as pw:
        options: dict[str, Any] = {"headless": True, "args": ["--no-sandbox"]}
        if executable:
            options["executable_path"] = executable
        browser = pw.chromium.launch(**options)
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
        page.on("pageerror", lambda e: errors.append(str(e)))
        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))
        if entry == "content":
            page.set_content((ROOT / "index.html").read_text(encoding="utf-8"))
        else:
            page.goto((ROOT / "index.html").as_uri())
        pw_expect(page.locator("#participation-status")).not_to_have_text("—")
        expect(not page.locator("#boot-error").is_visible(), "entrypoints", "HTML boot")
        for s in engine.scenarios:
            page.select_option("#scenario", s["id"])
            got = page.evaluate("JDDemo.getRecord()")
            engine.verify_record(got)
            expect(same(got, engine.record(engine.case(s["id"]))), "scenario_views", "browser " + s["id"])
            result = got["result"]
            candidate = got["input"]["plan"]["candidate"]
            expect(candidate + "社" in page.locator("#member-proposal").inner_text(),
                   "public_view_assertions", "candidate label " + s["id"])
            expected_tone = (result["participation"] if result["participation"] != "PASS" else
                             result["handoff"]["status"] if result["handoff"]["status"] != "PASS" else
                             {"POSITIVE": "PASS", "NEGATIVE": "REJECT", "BREAKEVEN": "HOLD", "UNKNOWN": "HOLD"}[result["business"]["status"]])
            expect(expected_tone in page.locator("#decision-banner").get_attribute("class").split(),
                   "public_view_assertions", "business/participation distinction " + s["id"])
            weights = [v["kg"] for v in got["input"]["plan"]["shipments"].values()]
            expected_load = "70% → " + ("未確認" if None in weights else
                (f"{sum(weights) / 4000 * 100:.1f}".rstrip("0").rstrip(".") + "%"))
            expect(page.locator("#load-change").inner_text() == expected_load,
                   "public_view_assertions", "derived load " + s["id"])
            payback = result["business"]["break_even_trips"]
            expect(page.locator("#payback-value").inner_text() == ("条件未成立" if not result["simulation_ready"] else "算定できません" if payback is None else str(payback) + "運行"),
                   "public_view_assertions", "payback is an input-derived assumption " + s["id"])
        page.select_option("#scenario", "JD01")
        page.click("#issue-permit"); page.click("#revoke"); page.click("#use-permit")
        expect("現在の参加" in page.locator("#gate-message").inner_text(), "interaction_assertions", "revocation gate")
        page.click("#refresh-planning"); page.click("#use-permit")
        expect("許可しました" in page.locator("#gate-message").inner_text(), "interaction_assertions", "restored current conditions")
        page.click("#use-permit")
        expect("使用済み" in page.locator("#gate-message").inner_text(), "interaction_assertions", "replay gate")
        page.click("#reset"); page.click("#issue-permit")
        page.fill("#candidate-kg", "800"); page.click("#apply")
        expect(page.locator("#participation-status").inner_text() == "HOLD", "interaction_assertions", "changed plan holds")
        page.click("#refresh-planning")
        expect(page.locator("#participation-status").inner_text() == "PASS" and page.locator("#handoff-status").inner_text() == "HOLD", "interaction_assertions", "separate handoff")
        page.click("#refresh-handoff")
        expect(page.locator("#handoff-status").inner_text() == "PASS", "interaction_assertions", "fresh handoff")
        page.click("#use-permit")
        expect("計画が変更" in page.locator("#gate-message").inner_text(), "interaction_assertions", "old permit blocked")
        page.click("#issue-permit"); page.click("#use-permit")
        expect("許可しました" in page.locator("#gate-message").inner_text(), "interaction_assertions", "new permit")
        page.fill("#revenue", "19000"); page.click("#apply"); page.click("#issue-permit"); page.click("#use-permit")
        expect("使用済み" in page.locator("#gate-message").inner_text(), "interaction_assertions", "economic edit does not reset nonce")
        page.click("#reset"); page.fill("#candidate-kg", ""); page.click("#apply"); page.click("#refresh-planning")
        expect(page.locator("#participation-status").inner_text() == "HOLD", "interaction_assertions", "unknown does not become confirmed")
        page.select_option("#scenario", "JD32")
        expect(page.locator("#participation-status").inner_text() == "PASS" and "-2,000" in page.locator("#profit").inner_text(), "interaction_assertions", "loss independent of admission")
        page.select_option("#scenario", "JD21")
        expect(page.locator("#download-summary").is_disabled(), "interaction_assertions", "unsafe disclosure disabled")
        page.select_option("#scenario", "JD02")
        with page.expect_download() as event:
            page.click("#download-record")
        file = Path(td) / "browser.json"; event.value.save_as(file)
        expect(engine.verify_record(read_json(file))["participation"] == "HOLD", "file_checks", "browser to Python")
        page.locator("#record-file").set_input_files(str(file))
        pw_expect(page.locator("#verify-message")).to_contain_text("再検証成立")
        expect("参加 HOLD" in page.locator("#verify-message").inner_text(), "file_checks", "HOLD not relabeled PASS")
        file = Path(td) / "python-suite.json"; write_json(file, engine.suite())
        page.locator("#record-file").set_input_files(str(file))
        pw_expect(page.locator("#verify-message")).to_contain_text("38記録")
        expect("38記録" in page.locator("#verify-message").inner_text(), "file_checks", "Python suite to browser")
        bad = engine.record(engine.case("JD01")); bad["result"]["participation"] = "HOLD"
        bad["record_sha256"] = digest({k: v for k, v in bad.items() if k != "record_sha256"})
        file = Path(td) / "tampered.json"; write_json(file, bad)
        page.locator("#record-file").set_input_files(str(file))
        pw_expect(page.locator("#verify-message")).to_contain_text("再検証不成立")
        expect("再検証不成立" in page.locator("#verify-message").inner_text(), "file_checks", "tampered import")
        page.click("#tamper-test")
        expect("改変を検出" in page.locator("#verify-message").inner_text(), "interaction_assertions", "tamper demonstration")
        page.select_option("#scenario", "JD01")
        with page.expect_download() as event:
            page.click("#download-summary")
        file = Path(td) / "summary.json"; event.value.save_as(file)
        expect(same(read_json(file), engine.partner_summary(engine.record(engine.case("JD01")))), "file_checks", "public summary")
        page.locator("#suite-details").evaluate("e=>e.open=true"); page.click("#run-suite")
        expect(page.locator("#suite-table tr").count() == 38 and "38 / 38" in page.locator("#suite-message").inner_text(), "interaction_assertions", "suite UI")
        with page.expect_download() as event:
            page.click("#download-suite")
        file = Path(td) / "browser-suite.json"; event.value.save_as(file)
        expect(engine.verify(read_json(file))["records"] == 38, "file_checks", "suite export")
        # Public business controls operate on existing schema fields only.
        for case_id, expected_status in [("JD02", "HOLD"), ("JD16", "REJECT"), ("JD32", "PASS"), ("JD01", "PASS")]:
            page.click('[data-case="' + case_id + '"]')
            expect(page.locator("#scenario").input_value() == case_id and
                   page.locator("#participation-status").inner_text() == expected_status and
                   page.locator('[data-case="' + case_id + '"]').get_attribute("aria-pressed") == "true",
                   "public_control_assertions", "scenario shortcut " + case_id)
        page.locator("details.assumptions").evaluate("e=>e.open=true")
        original_plan = page.evaluate("JDDemo.getCurrent().plan")
        for field, value in {"transport-cost": "8000", "coordination-cost": "5000", "system-cost": "2000",
                             "compensation-cost": "1000", "opportunity-cost": "3000", "initial-cost": "100000",
                             "manual-time": "30", "assisted-time": "40", "setup-time": "90"}.items():
            page.fill("#" + field, value)
        page.click("#apply")
        got = page.evaluate("JDDemo.getRecord()")
        engine.verify_record(got)
        expect(got["result"]["business"]["contribution_yen"] == -2000 and
               got["result"]["participation"] == "PASS" and same(got["input"]["plan"], original_plan),
               "public_control_assertions", "all costs change economics without changing approval targets")
        expect(page.locator("#workload-comparison").inner_text() == "30分 → 40分" and
               got["result"]["workload"]["saved_minutes"] == -10 and
               got["result"]["workload"]["setup_break_even_cases"] is None,
               "public_control_assertions", "increased workload is not a saving")
        page.fill("#transport-cost", ""); page.click("#apply")
        got = page.evaluate("JDDemo.getRecord()")
        engine.verify_record(got)
        expect(got["result"]["business"]["status"] == "UNKNOWN" and
               page.locator("#profit").inner_text() == "未確定" and
               "採算はまだ判断できない" in page.locator("#decision-headline").inner_text(),
               "public_control_assertions", "missing cost is never silently zero")
        unchanged = page.evaluate("JDDemo.getRecord()")
        for bad in ["1.5", "-1", "100000001"]:
            page.fill("#transport-cost", bad); page.click("#apply")
            expect(same(page.evaluate("JDDemo.getRecord()"), unchanged) and
                   "操作を反映しませんでした" in page.locator("#gate-message").inner_text(),
                   "public_control_assertions", "invalid assumption leaves record unchanged " + bad)
        page.click("#reset")
        page.fill("#initial-cost", ""); page.fill("#manual-time", ""); page.click("#apply")
        got = page.evaluate("JDDemo.getRecord()")
        engine.verify_record(got)
        expect(got["result"]["business"]["contribution_yen"] == 8000 and
               got["result"]["business"]["break_even_trips"] is None and
               page.locator("#payback-value").inner_text() == "算定できません" and
               page.locator("#workload-comparison").inner_text().startswith("未確認"),
               "public_control_assertions", "unknown initial cost and time remain unknown")
        with page.expect_download() as event:
            page.click("#download-record")
        file = Path(td) / "public-assumptions.json"; event.value.save_as(file)
        expect(same(read_json(file), got) and engine.verify_record(read_json(file))["verified"],
               "public_control_assertions", "edited business assumptions remain independently verifiable")
        page.click("#reset")
        expect(page.evaluate("new Set([...document.querySelectorAll('[id]')].map(x=>x.id)).size === document.querySelectorAll('[id]').length"),
               "public_control_assertions", "unique interface identifiers")
        for width, height in [(1440, 1100), (1024, 900), (768, 1024), (390, 844), (320, 740)]:
            page.set_viewport_size({"width": width, "height": height})
            expect(page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "viewports", "horizontal overflow")
        if entry == "file":
            handler = partial(QuietHandler, directory=str(ROOT))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                pw_expect(page.locator("#participation-status")).not_to_have_text("—")
                expect(page.locator("#participation-status").inner_text() == "PASS", "entrypoints", "HTTP boot")
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)
        counts["external_requests"] = sum(not (url.startswith("file://") or url.startswith("http://127.0.0.1:")) for url in requests)
        if errors or counts["external_requests"]:
            raise AssertionError("ブラウザエラー・外部通信: " + repr(errors) + repr(requests))
        browser.close()
    return {**counts, "entry_mode": entry}


if __name__ == "__main__":
    raise SystemExit(main())

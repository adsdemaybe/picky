import collections
import json
import sys

c = collections.Counter()
for line in open(sys.argv[1]):
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        continue
    t = e.get("type")
    c[t] += 1
    if t in ("plan.created", "plan.revised"):
        p = e["plan"]
        pi = p.get("planner") or p.get("info") or {}
        print(
            e.get("ts", "")[11:19] if isinstance(e.get("ts"), str) else "",
            t,
            p.get("plan_id"),
            "planner=",
            json.dumps(pi)[:300],
            len(p.get("steps", [])),
            "steps",
            e.get("reason", ""),
        )
    elif t == "step.started":
        s = e["step"]
        print(
            "  start",
            s.get("step_id"),
            s.get("kind"),
            (s.get("title") or s.get("label") or "")[:50],
            "tier=",
            e.get("tier"),
            "att=",
            e.get("attempt"),
        )
    elif t == "vla.status":
        st = e["status"]
        if st.get("phase") in ("accepted", "succeeded", "failed"):
            print(
                "    vla",
                e.get("step_id"),
                st.get("phase"),
                "tier=",
                st.get("tier"),
                "backend=",
                st.get("backend"),
                "fell_back_from=",
                st.get("fell_back_from"),
                "chunk=",
                st.get("chunk_no"),
                (st.get("message") or "")[:80],
                (st.get("error") or {}).get("code") if st.get("error") else "",
            )
        elif st.get("stage") == "observing" and "Perceptron" in (st.get("message") or ""):
            print("    look", e.get("step_id"), (st.get("message") or "")[:120])
    elif t == "vla.tier_changed":
        print("  TIER", json.dumps({k: v for k, v in e.items() if k not in ("seq", "id", "ts")})[:300])
    elif t == "step.verified":
        v = e["verdict"]
        pr = v.get("provider") or {}
        ats = [(a.get("op"), a.get("outcome"), a.get("decided_by")) for a in v.get("atoms", [])]
        print(
            "  verify",
            e.get("step_id"),
            e.get("phase"),
            "att=",
            e.get("attempt"),
            v.get("outcome"),
            "decided_by=",
            v.get("decided_by"),
            "provider=",
            pr.get("provider"),
            pr.get("model"),
            "caught=",
            e.get("caught_vla_false_success"),
            (v.get("reason") or "")[:70],
            ats if any(x[2] not in ("state", "ground_truth") for x in ats) else "",
        )
    elif t == "step.failed":
        print("  FAILED", e.get("step_id"), json.dumps(e.get("failure"))[:200], json.dumps(e.get("decision"))[:150])
    elif t == "step.completed":
        print("  done", e.get("step_id"), e.get("status"), e.get("attempts"))
    elif t == "error":
        print("ERROR", e.get("code"), str(e.get("message"))[:200])
    elif (
        t in ("checkout.completed", "plan.completed", "goal.completed", "goal.failed", "trip.updated")
        and t != "trip.updated"
    ):
        print(
            t,
            json.dumps({k: v for k, v in e.items() if k in ("status", "receipt", "goal_id", "plan_id", "reason")})[
                :200
            ],
        )
print(c.most_common(50))

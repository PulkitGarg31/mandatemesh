"""Command-line entry points. Secrets come only from .env; the agent never sees them."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm
from rich.table import Table

from mandatemesh.agent import LLMAgent, ScriptedAgent
from mandatemesh.evalset import run_eval
from mandatemesh.executor import FakeExecutor, RazorpayExecutor
from mandatemesh.fixtures import AGENT_ID, MERCHANT_ID
from mandatemesh.gate import Decision
from mandatemesh.keys import ROLES, Keys
from mandatemesh.ledger import Ledger, tamper
from mandatemesh.merchant import MockMerchant
from mandatemesh.orchestrator import SCENARIOS, Orchestrator, Scenario, inr
from mandatemesh.registry import AgentRegistry

KEYS_DIR = Path("keys")
RUNS_DIR = Path("runs")
console = Console()


def say(text: str) -> None:
    """Progress lines from the orchestrator: printed verbatim, never interpreted as markup."""
    console.print(text, markup=False, highlight=False)


def cmd_keys_init(args: argparse.Namespace) -> int:
    if KEYS_DIR.exists() and any(KEYS_DIR.iterdir()) and not args.force:
        console.print(f"[yellow]{KEYS_DIR}/ already has keys; use --force to regenerate[/]")
        return 1
    keys = Keys.generate()
    keys.save(KEYS_DIR)
    for role in ROLES:
        console.print(f"{role:9s} pub {keys.pub(role)}", markup=False)
    console.print(f"[green]wrote 4 Ed25519 private keys to {KEYS_DIR}/ (gitignored)[/]")
    return 0


def build_agent(mode: str, keys: Keys, sc: Scenario):
    if mode == "scripted":
        console.print("[dim]agent: scripted (deterministic, offline)[/]")
        return ScriptedAgent(AGENT_ID, keys.agent, [list(items) for items in sc.scripted_items])
    base_url, api_key, model = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_API_KEY"), os.environ.get("LLM_MODEL")
    if not (base_url and api_key and model):
        raise SystemExit("LLM_BASE_URL, LLM_API_KEY and LLM_MODEL must be set in .env (or pass --agent scripted)")
    console.print(f"[dim]agent: {escape(model)} via {escape(base_url)}[/]")
    return LLMAgent(AGENT_ID, keys.agent, base_url=base_url, api_key=api_key, model=model)


def build_executor(mode: str):
    if mode == "fake":
        outcomes = [o.strip() for o in os.environ.get("FAKE_OUTCOMES", "paid").split(",") if o.strip()]
        console.print(f"[dim]executor: fake, scripted outcomes {escape(str(outcomes))}[/]")
        return FakeExecutor(outcomes)
    key_id, secret = os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not (key_id.startswith("rzp_test_") and secret):
        raise SystemExit("RAZORPAY_KEY_ID (rzp_test_...) and RAZORPAY_KEY_SECRET must be set in .env (or pass --executor fake)")
    console.print("[dim]executor: Razorpay TEST mode (sole holder of the API keys)[/]")
    return RazorpayExecutor(key_id, secret)


def print_decision(d: Decision) -> None:
    table = Table(title=f"Gate decision: {d.verdict} ({d.rule_id})")
    table.add_column("rule")
    table.add_column("ok")
    table.add_column("detail")
    for c in d.checks:
        table.add_row(c.rule_id, "[green]yes[/]" if c.passed else "[red]NO[/]", escape(c.detail))
    console.print(table)
    console.print(d.reason, markup=False)


def print_ledger(ledger: Ledger) -> None:
    table = Table(title=f"Audit ledger: {escape(str(ledger.path))}")
    for col in ("seq", "type", "actor", "hash"):
        table.add_column(col)
    for e in ledger.events():
        table.add_row(str(e.seq), e.type, escape(e.actor), e.hash[:16] + "...")
    console.print(table)
    ok, bad = ledger.verify()
    console.print("[green]ledger chain verified[/]" if ok else f"[red]ledger chain BROKEN at seq {bad}[/]")


def cmd_demo(args: argparse.Namespace) -> int:
    load_dotenv()
    sc = SCENARIOS[args.scenario]
    console.rule(f"MandateMesh - scenario '{sc.name}'")
    console.print(sc.description, markup=False)
    keys = Keys.load(KEYS_DIR)
    agent = build_agent(args.agent, keys, sc)
    if args.scenario == "poison" and args.executor == "real" and args.auto_approve == "yes":
        raise SystemExit("refusing: poison with --auto-approve yes against the real executor would create an INR 30,000 link")
    executor = build_executor(args.executor)
    run_id = args.run_id or f"{sc.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise SystemExit("--run-id must be a plain directory name")
    ledger = Ledger(RUNS_DIR / run_id / "ledger.jsonl")

    def approver(cart, decision) -> bool:
        console.print(f"[yellow]STEP-UP required:[/] {escape(decision.reason)}")
        if args.auto_approve == "yes":
            console.print("auto-approve: yes")
            return True
        if args.auto_approve == "no":
            console.print("auto-approve: no")
            return False
        if not sys.stdin.isatty():
            console.print("stdin is not a terminal; declining step-up (pass --auto-approve yes|no)")
            return False
        try:
            return Confirm.ask(f"Approve {inr(cart.total_paise)} for cart {cart.cart_id}?", default=False)
        except EOFError:
            return False

    orch = Orchestrator(
        keys, AgentRegistry(), MockMerchant(MERCHANT_ID, keys.merchant), agent, executor, ledger, approver,
        say=say, poll_timeout_s=args.poll_timeout,
    )
    result = orch.run(sc)
    console.rule("result")
    if result.decision is not None:
        print_decision(result.decision)
    print_ledger(ledger)
    if result.outcome == "paid" and result.payment_id:
        receipt_path = ledger.path.parent / f"receipt-{result.payment_id}.md"
        receipt_path.write_text(ledger.receipt(result.payment_id), encoding="utf-8")
        console.print(f"[green]receipt written to {escape(str(receipt_path))}[/]")
    console.print(f"outcome: {result.outcome}   ledger: {ledger.path}", markup=False)
    return 1 if result.outcome == "error" else 0


def cmd_ledger(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        console.print(f"[red]no ledger at {escape(str(path))}[/]")
        return 2
    if args.ledger_cmd == "verify":
        ok, bad = Ledger(path).verify()
        console.print("[green]ledger chain verified[/]" if ok else f"[red]ledger chain BROKEN at seq {bad}[/]")
        return 0 if ok else 2
    if args.ledger_cmd == "receipt":
        console.print(Ledger(path).receipt(args.payment_id), markup=False)
        return 0
    tamper(path, args.seq)
    console.print(f"[yellow]edited seq {args.seq} in {escape(str(path))} without re-hashing; now run: ledger verify[/]")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    m = run_eval()
    table = Table(title="Gate abuse eval (offline, deterministic, hand-built cases)")
    for col in ("case", "expected", "verdict", "rule", "correct"):
        table.add_column(col)
    for r in m["rows"]:
        table.add_row(r.name, "blocked" if r.expect_blocked else "allowed", r.verdict, r.rule_id, "[green]yes[/]" if r.correct else "[red]NO[/]")
    console.print(table)
    console.print(f"poisoned blocked: {m['blocked']}/{m['poisoned']}  block_rate = {m['block_rate']:.0%}")
    console.print(f"benign wrongly blocked: {m['false_positives']}/{m['benign']}  false_positive_rate = {m['false_positive_rate']:.0%}")
    console.print("blocked = DENY or STEP_UP; these are one-per-attack-class hand-built cases, not a sampled distribution", markup=False)
    return 0 if all(r.correct for r in m["rows"]) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mandatemesh", description="The LLM proposes, the deterministic gate disposes.")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keys", help="manage signing keys")
    ks = k.add_subparsers(dest="keys_cmd", required=True)
    ki = ks.add_parser("init", help="generate user/agent/merchant/gate keys into ./keys")
    ki.add_argument("--force", action="store_true")
    ki.set_defaults(func=cmd_keys_init)

    d = sub.add_parser("demo", help="run one scenario end to end")
    d.add_argument("--scenario", choices=sorted(SCENARIOS), default="happy")
    d.add_argument("--agent", choices=["llm", "scripted"], default="llm")
    d.add_argument("--executor", choices=["real", "fake"], default="real")
    d.add_argument("--auto-approve", choices=["ask", "yes", "no"], default="ask")
    d.add_argument("--run-id")
    d.add_argument("--poll-timeout", type=int, default=180, help="seconds to wait per payment attempt")
    d.set_defaults(func=cmd_demo)

    l = sub.add_parser("ledger", help="verify, export or tamper a ledger")
    ls = l.add_subparsers(dest="ledger_cmd", required=True)
    v = ls.add_parser("verify")
    v.add_argument("path")
    v.set_defaults(func=cmd_ledger)
    r = ls.add_parser("receipt")
    r.add_argument("path")
    r.add_argument("payment_id")
    r.set_defaults(func=cmd_ledger)
    t = ls.add_parser("tamper")
    t.add_argument("path")
    t.add_argument("seq", type=int)
    t.set_defaults(func=cmd_ledger)

    e = sub.add_parser("eval", help="run the abuse eval and print block rate / false-positive rate")
    e.set_defaults(func=cmd_eval)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/]")
        return 130
    except (FileNotFoundError, KeyError, ValueError, PermissionError) as exc:
        console.print(f"[red]error:[/] {escape(str(exc.args[0] if exc.args else exc))}")
        return 2

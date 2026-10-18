#!/usr/bin/env python3
"""Offline static audit for Brazy Bot. Uses only Python's standard library."""
import ast
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "brazy_bot.py")
source = path.read_text(encoding="utf-8")
tree = ast.parse(source, filename=str(path))
functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
errors = []
passed = []

def check(ok, label):
    (passed if ok else errors).append(label)

check("main" in functions and "_button_dispatch" in functions, "main and callback dispatcher exist")
main = functions.get("main")
commands = []
if main:
    for n in ast.walk(main):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_handler" and n.args:
            a = n.args[0]
            if isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "CommandHandler":
                try:
                    commands.append((ast.literal_eval(a.args[0]), ast.unparse(a.args[1])))
                except Exception:
                    pass
for command, callback in commands:
    check(callback in functions, f"/{command} callback {callback} is defined")
required = {"start", "stocks", "crypto", "toggle", "account", "cryptoscan", "snipe", "pos", "wallet", "withdraw", "norug", "antimev"}
registered = {name for name, _ in commands}
for name in sorted(required):
    check(name in registered, f"required command /{name} is registered")

callback_values = []
for n in ast.walk(tree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "InlineKeyboardButton":
        kwargs = {kw.arg: kw.value for kw in n.keywords}
        if "callback_data" in kwargs:
            try:
                callback_values.append(ast.literal_eval(kwargs["callback_data"]))
            except Exception:
                pass

dispatch = functions.get("_button_dispatch")
equals, tuple_values, prefixes = set(), set(), set()
if dispatch:
    for n in ast.walk(dispatch):
        if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "data":
            for op, value in zip(n.ops, n.comparators):
                if isinstance(op, ast.Eq):
                    try: equals.add(ast.literal_eval(value))
                    except Exception: pass
                if isinstance(op, ast.In) and isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                    for item in value.elts:
                        try: tuple_values.add(ast.literal_eval(item))
                        except Exception: pass
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "data" and n.func.attr == "startswith":
            try: prefixes.add(ast.literal_eval(n.args[0]))
            except Exception: pass
uncovered = [v for v in callback_values if isinstance(v, str) and v not in equals and v not in tuple_values and not any(v.startswith(p) for p in prefixes)]
check(not uncovered, "all statically defined keyboard callbacks match a dispatcher route (or fallback)")

checks = {
    "per-user JSON profile path": 'return USERS_DIR / f"{int(user_id)}.json"' in source,
    "global authorization gate registered": 'app.add_handler(TypeHandler(Update, authorization_gate), group=-1)' in source,
    "button exceptions surfaced": 'await safe_edit(query, "Button failed: %s" % e)' in source,
    "unknown callbacks surfaced": "Unhandled callback button" in source,
    "global update error handler registered": "app.add_error_handler(telegram_error_handler)" in source,
    "message Markdown parse fallback": "ExtBot.send_message = _send_message_with_parse_fallback" in source,
    "edit Markdown parse fallback": "ExtBot.edit_message_text = _edit_message_with_parse_fallback" in source,
    "photo analysis errors surfaced": "Chart photo analysis failed for user" in source,
    "withdraw finite positive amount validation": 'isfinite(amount)' in source and "Amount must be a finite number greater than zero" in source,
    "AI text provider fallback": 'providers.append("openai")' in source and "trying next configured provider" in source,
    "chart vision calls do not block event loop": "await asyncio.to_thread(\n                anthropic_client.messages.create" in source,
}
for label, ok in checks.items(): check(ok, label)

print(f"File: {path}")
print(f"Functions: {len(functions)} | registered commands: {len(commands)} | keyboard callbacks: {len(callback_values)}")
for label in passed: print("PASS  " + label)
for label in errors: print("FAIL  " + label)
print(f"\nResult: {len(passed)} passed, {len(errors)} failed")
sys.exit(1 if errors else 0)

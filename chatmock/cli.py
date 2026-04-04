from __future__ import annotations

import errno
import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime

from .app import create_app
from .config import CLIENT_ID_DEFAULT
from .limits import RateLimitWindow, compute_reset_at, load_rate_limit_snapshot
from .oauth import OAuthHTTPServer, OAuthHandler, REQUIRED_PORT, URL_BASE
from .utils import (
    eprint,
    get_active_profile_name,
    get_auth_profile,
    get_chatgpt_local_home_dir,
    get_effective_profile_name,
    get_home_dir,
    list_auth_profiles,
    load_chatgpt_tokens,
    parse_jwt_claims,
    read_auth_file,
    read_chatgpt_local_auth_file,
    set_active_profile,
)


_STATUS_LIMIT_BAR_SEGMENTS = 30
_STATUS_LIMIT_BAR_FILLED = "█"
_STATUS_LIMIT_BAR_EMPTY = "░"
_STATUS_LIMIT_BAR_PARTIAL = "▓"


def _clamp_percent(value: float) -> float:
    try:
        percent = float(value)
    except Exception:
        return 0.0
    if percent != percent:
        return 0.0
    if percent < 0.0:
        return 0.0
    if percent > 100.0:
        return 100.0
    return percent


def _render_progress_bar(percent_used: float) -> str:
    ratio = max(0.0, min(1.0, percent_used / 100.0))
    filled_exact = ratio * _STATUS_LIMIT_BAR_SEGMENTS
    filled = int(filled_exact)
    partial = filled_exact - filled
    
    has_partial = partial > 0.5
    if has_partial:
        filled += 1
    
    filled = max(0, min(_STATUS_LIMIT_BAR_SEGMENTS, filled))
    empty = _STATUS_LIMIT_BAR_SEGMENTS - filled
    
    if has_partial and filled > 0:
        bar = _STATUS_LIMIT_BAR_FILLED * (filled - 1) + _STATUS_LIMIT_BAR_PARTIAL + _STATUS_LIMIT_BAR_EMPTY * empty
    else:
        bar = _STATUS_LIMIT_BAR_FILLED * filled + _STATUS_LIMIT_BAR_EMPTY * empty
    
    return f"[{bar}]"


def _get_usage_color(percent_used: float) -> str:
    if percent_used >= 90:
        return "\033[91m" 
    elif percent_used >= 75:
        return "\033[93m"  
    elif percent_used >= 50:
        return "\033[94m"  
    else:
        return "\033[92m" 


def _reset_color() -> str:
    """ANSI reset color code"""
    return "\033[0m"


def _format_window_duration(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    try:
        total = int(minutes)
    except Exception:
        return None
    if total <= 0:
        return None
    minutes = total
    weeks, remainder = divmod(minutes, 7 * 24 * 60)
    days, remainder = divmod(remainder, 24 * 60)
    hours, remainder = divmod(remainder, 60)
    parts = []
    if weeks:
        parts.append(f"{weeks} week" + ("s" if weeks != 1 else ""))
    if days:
        parts.append(f"{days} day" + ("s" if days != 1 else ""))
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if remainder:
        parts.append(f"{remainder} minute" + ("s" if remainder != 1 else ""))
    if not parts:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    return " ".join(parts)


def _format_reset_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    try:
        value = int(seconds)
    except Exception:
        return None
    if value < 0:
        value = 0
    days, remainder = divmod(value, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, remainder = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts and remainder:
        parts.append("under 1m")
    if not parts:
        parts.append("0m")
    return " ".join(parts)


def _format_local_datetime(dt: datetime) -> str:
    local = dt.astimezone()
    tz_name = local.tzname() or "local"
    return f"{local.strftime('%b %d, %Y %H:%M')} {tz_name}"


def _resolve_profile_name(profile_name: str | None) -> str | None:
    if isinstance(profile_name, str) and profile_name.strip():
        return profile_name.strip()
    return get_effective_profile_name()


def _account_summary(profile_name: str | None = None) -> dict[str, str | None] | None:
    access_token, account_id, id_token = load_chatgpt_tokens(profile_name=profile_name)
    if not access_token or not id_token:
        return None
    id_claims = parse_jwt_claims(id_token) or {}
    access_claims = parse_jwt_claims(access_token) or {}
    email = id_claims.get("email") or id_claims.get("preferred_username") or "<unknown>"
    plan_raw = (access_claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type") or "unknown"
    plan_map = {
        "plus": "Plus",
        "pro": "Pro",
        "free": "Free",
        "team": "Team",
        "enterprise": "Enterprise",
    }
    plan = plan_map.get(str(plan_raw).lower(), str(plan_raw).title() if isinstance(plan_raw, str) else "Unknown")
    return {
        "email": email,
        "plan": plan,
        "account_id": account_id,
    }


def _print_usage_limits_block(profile_name: str | None = None) -> None:
    stored = load_rate_limit_snapshot(profile_name=profile_name)
    
    print("📊 Usage Limits")
    
    if stored is None:
        print("  No usage data available yet. Send a request through ChatMock first.")
        print()
        return

    update_time = _format_local_datetime(stored.captured_at)
    print(f"Last updated: {update_time}")
    print()

    windows: list[tuple[str, str, RateLimitWindow]] = []
    if stored.snapshot.primary is not None:
        windows.append(("⚡", "5 hour limit", stored.snapshot.primary))
    if stored.snapshot.secondary is not None:
        windows.append(("📅", "Weekly limit", stored.snapshot.secondary))

    if not windows:
        print("  Usage data was captured but no limit windows were provided.")
        print()
        return

    for i, (icon_label, desc, window) in enumerate(windows):
        if i > 0:
            print()
        
        percent_used = _clamp_percent(window.used_percent)
        remaining = max(0.0, 100.0 - percent_used)
        color = _get_usage_color(percent_used)
        reset = _reset_color()
        
        progress = _render_progress_bar(percent_used)
        usage_text = f"{percent_used:5.1f}% used"
        remaining_text = f"{remaining:5.1f}% left"
        
        print(f"{icon_label} {desc}")
        print(f"{color}{progress}{reset} {color}{usage_text}{reset} | {remaining_text}")
        
        reset_in = _format_reset_duration(window.resets_in_seconds)
        reset_at = compute_reset_at(stored.captured_at, window)
        
        if reset_in and reset_at:
            reset_at_str = _format_local_datetime(reset_at)
            print(f"    ⏳ Resets in: {reset_in} at {reset_at_str}")
        elif reset_in:
            print(f"    ⏳ Resets in: {reset_in}")
        elif reset_at:
            reset_at_str = _format_local_datetime(reset_at)
            print(f"    ⏳ Resets at: {reset_at_str}")

    print()

def cmd_login(no_browser: bool, verbose: bool, profile_name: str | None = None) -> int:
    home_dir = get_home_dir()
    client_id = CLIENT_ID_DEFAULT
    if not client_id:
        eprint("ERROR: No OAuth client id configured. Set CHATGPT_LOCAL_CLIENT_ID.")
        return 1

    try:
        bind_host = os.getenv("CHATGPT_LOCAL_LOGIN_BIND", "127.0.0.1")
        httpd = OAuthHTTPServer((bind_host, REQUIRED_PORT), OAuthHandler, home_dir=home_dir, client_id=client_id, profile_name=profile_name, verbose=verbose)
    except OSError as e:
        eprint(f"ERROR: {e}")
        if e.errno == errno.EADDRINUSE:
            return 13
        return 1

    auth_url = httpd.auth_url()
    with httpd:
        eprint(f"Starting local login server on {URL_BASE}")
        if not no_browser:
            try:
                webbrowser.open(auth_url, new=1, autoraise=True)
            except Exception as e:
                eprint(f"Failed to open browser: {e}")
        eprint(f"If your browser did not open, navigate to:\n{auth_url}")

        def _stdin_paste_worker() -> None:
            try:
                eprint(
                    "If the browser can't reach this machine, paste the full redirect URL here and press Enter (or leave blank to keep waiting):"
                )
                line = sys.stdin.readline().strip()
                if not line:
                    return
                try:
                    from urllib.parse import urlparse, parse_qs

                    parsed = urlparse(line)
                    params = parse_qs(parsed.query)
                    code = (params.get("code") or [None])[0]
                    state = (params.get("state") or [None])[0]
                    if not code:
                        eprint("Input did not contain an auth code. Ignoring.")
                        return
                    if state and state != httpd.state:
                        eprint("State mismatch. Ignoring pasted URL for safety.")
                        return
                    eprint("Received redirect URL. Completing login without callback…")
                    bundle, _ = httpd.exchange_code(code)
                    if httpd.persist_auth(bundle):
                        httpd.exit_code = 0
                        eprint("Login successful. Tokens saved.")
                    else:
                        eprint("ERROR: Unable to persist auth file.")
                    httpd.shutdown()
                except Exception as exc:
                    eprint(f"Failed to process pasted redirect URL: {exc}")
            except Exception:
                pass

        try:
            import threading

            threading.Thread(target=_stdin_paste_worker, daemon=True).start()
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            eprint("\nKeyboard interrupt received, exiting.")
        return httpd.exit_code


def cmd_accounts_list() -> int:
    auth = read_chatgpt_local_auth_file()
    profiles = list_auth_profiles(auth)
    active_profile = get_active_profile_name(auth)
    home_dir = get_chatgpt_local_home_dir()
    print(f"Profiles home: {home_dir}")
    if not profiles:
        print("No chatgpt-local profiles found.")
        return 0
    print("")
    for profile in profiles:
        summary = _account_summary(profile)
        marker = "*" if profile == active_profile else " "
        print(f"{marker} {profile}")
        if summary is None:
            print("  • Not signed in")
            continue
        print(f"  • Login: {summary['email']}")
        print(f"  • Plan: {summary['plan']}")
        if summary.get("account_id"):
            print(f"  • Account ID: {summary['account_id']}")
    return 0


def cmd_accounts_use(profile_name: str) -> int:
    if set_active_profile(profile_name):
        print(f"Active profile: {profile_name}")
        return 0
    eprint(f"ERROR: profile not found: {profile_name}")
    return 1


def cmd_info(profile_name: str | None = None, json_output: bool = False) -> int:
    resolved_profile = _resolve_profile_name(profile_name)
    if json_output:
        if resolved_profile is not None:
            auth = get_auth_profile(read_chatgpt_local_auth_file(), resolved_profile)
        else:
            auth = read_auth_file()
        print(json.dumps(auth or {}, indent=2))
        return 0

    summary = _account_summary(resolved_profile)
    print("👤 Account")
    if summary is None:
        print("  • Not signed in")
        print("  • Run: python3 chatmock.py login")
        print("")
        _print_usage_limits_block(resolved_profile)
        return 0

    if resolved_profile is not None:
        print(f"  • Profile: {resolved_profile}")
    print("  • Signed in with ChatGPT")
    print(f"  • Login: {summary['email']}")
    print(f"  • Plan: {summary['plan']}")
    if summary.get("account_id"):
        print(f"  • Account ID: {summary['account_id']}")
    print("")
    _print_usage_limits_block(resolved_profile)
    return 0


def cmd_serve(
    host: str,
    port: int,
    verbose: bool,
    verbose_obfuscation: bool,
    reasoning_effort: str,
    reasoning_summary: str,
    reasoning_compat: str,
    fast_mode: bool,
    debug_model: str | None,
    expose_reasoning_models: bool,
    default_web_search: bool,
    profile_name: str | None = None,
) -> int:
    app = create_app(
        verbose=verbose,
        verbose_obfuscation=verbose_obfuscation,
        reasoning_effort=reasoning_effort,
        reasoning_summary=reasoning_summary,
        reasoning_compat=reasoning_compat,
        fast_mode=fast_mode,
        debug_model=debug_model,
        expose_reasoning_models=expose_reasoning_models,
        default_web_search=default_web_search,
        auth_profile=profile_name,
    )

    app.run(host=host, debug=False, use_reloader=False, port=port, threaded=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ChatMock: login & OpenAI-compatible proxy")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="Authorize with ChatGPT and store tokens")
    p_login.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    p_login.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    p_login.add_argument("--profile", help="Store login under a named chatgpt-local profile")

    p_serve = sub.add_parser("serve", help="Run local OpenAI-compatible server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    p_serve.add_argument(
        "--verbose-obfuscation",
        action="store_true",
        help="Also dump raw SSE/obfuscation events (in addition to --verbose request/response logs).",
    )
    p_serve.add_argument(
        "--debug-model",
        dest="debug_model",
        default=os.getenv("CHATGPT_LOCAL_DEBUG_MODEL"),
        help="Forcibly override requested 'model' with this value",
    )
    p_serve.add_argument(
        "--fast-mode",
        action=argparse.BooleanOptionalAction,
        default=(os.getenv("CHATGPT_LOCAL_FAST_MODE") or "").strip().lower() in ("1", "true", "yes", "on"),
        help="Enable GPT fast mode by default for supported models; request-level overrides still take precedence.",
    )
    p_serve.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
        default=os.getenv("CHATGPT_LOCAL_REASONING_EFFORT", "medium").lower(),
        help="Reasoning effort level for Responses API (default: medium)",
    )
    p_serve.add_argument(
        "--reasoning-summary",
        choices=["auto", "concise", "detailed", "none"],
        default=os.getenv("CHATGPT_LOCAL_REASONING_SUMMARY", "auto").lower(),
        help="Reasoning summary verbosity (default: auto)",
    )
    p_serve.add_argument(
        "--reasoning-compat",
        choices=["legacy", "o3", "think-tags", "current"],
        default=os.getenv("CHATGPT_LOCAL_REASONING_COMPAT", "think-tags").lower(),
        help=(
            "Compatibility mode for exposing reasoning to clients (legacy|o3|think-tags). "
            "'current' is accepted as an alias for 'legacy'"
        ),
    )
    p_serve.add_argument(
        "--expose-reasoning-models",
        action="store_true",
        default=(os.getenv("CHATGPT_LOCAL_EXPOSE_REASONING_MODELS") or "").strip().lower() in ("1", "true", "yes", "on"),
        help=(
            "Expose GPT-5 family reasoning effort variants (none|minimal|low|medium|high|xhigh where supported) "
            "as separate models from /v1/models. This allows choosing effort via model selection in compatible UIs."
        ),
    )
    p_serve.add_argument(
        "--enable-web-search",
        action=argparse.BooleanOptionalAction,
        default=(os.getenv("CHATGPT_LOCAL_ENABLE_WEB_SEARCH") or "").strip().lower() in ("1", "true", "yes", "on"),
        help=(
            "Enable default web_search tool when a request omits responses_tools (off by default). "
            "Also configurable via CHATGPT_LOCAL_ENABLE_WEB_SEARCH."
        ),
    )
    p_serve.add_argument("--profile", help="Use a named chatgpt-local profile for this server process")

    p_info = sub.add_parser("info", help="Print current stored tokens and derived account id")
    p_info.add_argument("--json", action="store_true", help="Output raw auth.json contents")
    p_info.add_argument("--profile", help="Read a named chatgpt-local profile without changing the active profile")

    p_accounts = sub.add_parser("accounts", help="Manage chatgpt-local profiles")
    accounts_sub = p_accounts.add_subparsers(dest="accounts_command", required=True)
    accounts_sub.add_parser("list", help="List chatgpt-local profiles")
    p_accounts_use = accounts_sub.add_parser("use", help="Set the active chatgpt-local profile")
    p_accounts_use.add_argument("profile")

    args = parser.parse_args()

    if args.command == "login":
        sys.exit(cmd_login(no_browser=args.no_browser, verbose=args.verbose, profile_name=args.profile))
    elif args.command == "serve":
        sys.exit(
            cmd_serve(
                host=args.host,
                port=args.port,
                verbose=args.verbose,
                verbose_obfuscation=args.verbose_obfuscation,
                reasoning_effort=args.reasoning_effort,
                reasoning_summary=args.reasoning_summary,
                reasoning_compat=args.reasoning_compat,
                fast_mode=args.fast_mode,
                debug_model=args.debug_model,
                expose_reasoning_models=args.expose_reasoning_models,
                default_web_search=args.enable_web_search,
                profile_name=args.profile,
            )
        )
    elif args.command == "info":
        sys.exit(cmd_info(profile_name=args.profile, json_output=args.json))
    elif args.command == "accounts":
        if args.accounts_command == "list":
            sys.exit(cmd_accounts_list())
        if args.accounts_command == "use":
            sys.exit(cmd_accounts_use(args.profile))
        parser.error("Unknown accounts command")
    else:
        parser.error("Unknown command")


if __name__ == "__main__":
    main()

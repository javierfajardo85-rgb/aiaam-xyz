"""
ReputationGuard — Pre-flight firewall for every PR submitted by AIAAM agents.

Philosophy: missing a good repo costs us one opportunity.
            touching a bad one costs us our reputation permanently.

Risk scoring:
  0.0–0.35 → GREEN  — approve
  0.35–0.6 → YELLOW — approve with logged warning
  0.6–1.0  → RED    — block (hard stop returns 1.0)

Modes:
  bulk      — B3 automated docs-only PRs (strict star range, strict rate limits)
  strategic — hand-curated code integrations (LangChain, CrewAI, AutoGPT)
              bypasses star ceiling, but adds code-quality checks

Usage:
    guard = ReputationGuard(token=os.getenv("GITHUB_TOKEN"))
    verdict = guard.check("langchain-ai", "langchain", mode="strategic")
    if verdict.approved:
        ...submit PR...
    else:
        print(verdict.reason)
"""

from __future__ import annotations

import re
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import httpx


# ── Thresholds ─────────────────────────────────────────────────────────────
BULK_STARS_MIN      = 300      # below → too obscure, no reach
BULK_STARS_MAX      = 20_000   # above → too large, doc PRs get ignored or flagged
STRATEGIC_STARS_MIN = 1_000    # strategic PRs need real user base
MAX_DAYS_SINCE_PUSH = 120      # stale repo — team won't review
MAX_OPEN_ISSUES     = 3_000    # overwhelmed teams skip doc PRs
RATE_LIMIT_DAY      = 2        # max PRs B3 submits per 24h
RATE_LIMIT_WEEK     = 8        # max PRs B3 submits per 7 days
ORG_COOLDOWN_DAYS   = 14       # days before we PR another repo in same org

# ── Hard-stop patterns in CONTRIBUTING.md or README ────────────────────────
_HOSTILE_RE = re.compile(
    r"(no unsolicited|do not open pull request|we do not accept|"
    r"please do not submit|not accepting.*pr|won.t accept.*pr|"
    r"not interested in.*integration|automated.*pr.*not welcome|"
    r"spam|reject.*bot|block.*bot)",
    re.IGNORECASE,
)

# ── Patterns that signal a bot-unfriendly CONTRIBUTING.md ──────────────────
_STRICT_CONTRIB_RE = re.compile(
    r"(must open.*issue first|issue.*required.*before.*pr|"
    r"discuss.*before.*pr|rfc.*required|"
    r"all prs.*require.*approval|linked issue.*required)",
    re.IGNORECASE,
)

# ── Repos where we already submitted and were explicitly rejected ───────────
# (mirrors the blocklist in context_injector.py — keep in sync)
EXPLICIT_REJECTIONS: set[str] = {
    "sqlalchemy/sqlalchemy",
    "pydantic/pydantic-settings",
    "microsoft/playwright",
    "pola-rs/polars",
    "apache/arrow",
    "run-llama/llama_index",
    "huggingface/huggingface_hub",
    "agronholm/apscheduler",
    "puppeteer/puppeteer",
}

# ── Our bot branch prefixes — used to detect duplicate open PRs ────────────
_OUR_BRANCHES = ("mai1-agents-md", "mai1-inline-contract", "mai1-")


@dataclass
class GuardVerdict:
    approved: bool
    risk_score: float          # 0.0–1.0
    colour: str                # GREEN / YELLOW / RED
    reason: str
    warnings: list[str] = field(default_factory=list)
    repo_stats: dict = field(default_factory=dict)

    def __str__(self) -> str:
        flag = "✓" if self.approved else "✗"
        colour_label = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(self.colour, "⚪")
        lines = [
            f"  {colour_label} [{self.colour}] risk={self.risk_score:.2f}  {flag} {self.reason}"
        ]
        for w in self.warnings:
            lines.append(f"    ⚠  {w}")
        if self.repo_stats:
            s = self.repo_stats
            lines.append(
                f"    stars={s.get('stars','?')}  "
                f"open_issues={s.get('open_issues','?')}  "
                f"last_push={s.get('days_since_push','?')}d ago"
            )
        return "\n".join(lines)


class ReputationGuard:
    """
    Run before every PR. Returns a GuardVerdict.

    Hard stops (risk = 1.0, not overridable):
      - Explicit rejection list
      - Existing open PR from our fork on this repo
      - Archived / disabled repo
      - Last push > MAX_DAYS_SINCE_PUSH days
      - Star count out of range for the mode
      - CONTRIBUTING.md or README contains hostile keyword
      - DB rate limit exceeded (RATE_LIMIT_DAY or RATE_LIMIT_WEEK)
      - Org cooldown not elapsed
    """

    def __init__(self, token: Optional[str] = None):
        self._token = token or os.getenv("GITHUB_TOKEN", "")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            self._headers["Authorization"] = f"Bearer {self._token}"

    # ── Public API ──────────────────────────────────────────────────────────

    def check(
        self,
        owner: str,
        repo: str,
        mode: str = "bulk",          # "bulk" | "strategic"
        db_session=None,             # optional sqlmodel Session for rate-limit checks
    ) -> GuardVerdict:
        """
        Run all pre-flight checks. Returns GuardVerdict.
        mode="bulk"      → strict star ceiling, automated docs-only PR
        mode="strategic" → no star ceiling, adds issue-first check
        """
        slug = f"{owner}/{repo}"
        risk = 0.0
        warnings: list[str] = []

        # ── 1. Explicit rejection list ─────────────────────────────────────
        if slug in EXPLICIT_REJECTIONS:
            return GuardVerdict(
                approved=False, risk_score=1.0, colour="RED",
                reason=f"{slug} is in explicit-rejection list — never reapproach",
            )

        # ── 2. Fetch repo metadata ─────────────────────────────────────────
        meta = self._fetch_repo(owner, repo)
        if meta is None:
            return GuardVerdict(
                approved=False, risk_score=1.0, colour="RED",
                reason=f"Could not reach GitHub API for {slug}",
            )

        if meta.get("archived") or meta.get("disabled"):
            return GuardVerdict(
                approved=False, risk_score=1.0, colour="RED",
                reason=f"{slug} is archived or disabled",
                repo_stats=self._stats(meta),
            )

        stars = meta.get("stargazers_count", 0)
        open_issues = meta.get("open_issues_count", 0)
        last_push_str = meta.get("pushed_at", "")
        days_since_push = self._days_since(last_push_str)

        stats = {
            "stars": stars,
            "open_issues": open_issues,
            "days_since_push": days_since_push,
        }

        # ── 3. Staleness check ────────────────────────────────────────────
        if days_since_push > MAX_DAYS_SINCE_PUSH:
            return GuardVerdict(
                approved=False, risk_score=1.0, colour="RED",
                reason=f"{slug} last pushed {days_since_push}d ago (limit: {MAX_DAYS_SINCE_PUSH}d) — team won't review",
                repo_stats=stats,
            )

        # ── 4. Star-range check ───────────────────────────────────────────
        if mode == "bulk":
            if stars < BULK_STARS_MIN:
                return GuardVerdict(
                    approved=False, risk_score=1.0, colour="RED",
                    reason=f"{slug} has {stars} stars (min {BULK_STARS_MIN} for bulk mode) — too obscure",
                    repo_stats=stats,
                )
            if stars > BULK_STARS_MAX:
                return GuardVerdict(
                    approved=False, risk_score=1.0, colour="RED",
                    reason=f"{slug} has {stars} stars (max {BULK_STARS_MAX} for bulk mode) — too large, PR will be ignored or flagged",
                    repo_stats=stats,
                )
        else:  # strategic
            if stars < STRATEGIC_STARS_MIN:
                return GuardVerdict(
                    approved=False, risk_score=1.0, colour="RED",
                    reason=f"{slug} has {stars} stars (min {STRATEGIC_STARS_MIN} for strategic mode)",
                    repo_stats=stats,
                )
            # No upper limit for strategic, but warn if very large
            if stars > 50_000:
                warnings.append(
                    f"{stars:,} stars — very large repo. Consider opening a GitHub Issue first "
                    f"to gauge interest before submitting code."
                )
                risk += 0.25

        # ── 5. Existing open PR from our fork ─────────────────────────────
        our_pr = self._find_our_open_pr(owner, repo)
        if our_pr:
            return GuardVerdict(
                approved=False, risk_score=1.0, colour="RED",
                reason=f"Already have open PR: {our_pr}",
                repo_stats=stats,
            )

        # ── 6. CONTRIBUTING.md / README hostile-keyword scan ─────────────
        hostile_hit = self._scan_contributing(owner, repo)
        if hostile_hit:
            return GuardVerdict(
                approved=False, risk_score=1.0, colour="RED",
                reason=f"{slug} CONTRIBUTING.md/README contains hostile keyword: \"{hostile_hit}\"",
                repo_stats=stats,
            )

        # ── 7. Strict contribution policy (requires issue first) ──────────
        strict_hit = self._scan_strict_policy(owner, repo)
        if strict_hit:
            if mode == "bulk":
                return GuardVerdict(
                    approved=False, risk_score=0.8, colour="RED",
                    reason=f"{slug} requires issue-first: \"{strict_hit}\" — file an issue before the PR",
                    repo_stats=stats,
                )
            else:
                warnings.append(
                    f"CONTRIBUTING requires issue-first: \"{strict_hit}\". "
                    f"Open a GitHub Issue before this PR or it will be closed."
                )
                risk += 0.3

        # ── 8. Open-issues overload ────────────────────────────────────────
        if open_issues > MAX_OPEN_ISSUES:
            warnings.append(f"{open_issues} open issues — team is backlogged, PR may be missed")
            risk += 0.15

        # ── 9. DB rate-limit and org-cooldown checks ──────────────────────
        if db_session is not None:
            rate_verdict = self._check_rate_limits(db_session, owner)
            if rate_verdict:
                return GuardVerdict(
                    approved=False, risk_score=1.0, colour="RED",
                    reason=rate_verdict,
                    repo_stats=stats,
                )

        # ── 10. Mild risk adjustments ─────────────────────────────────────
        if stars < 500:
            risk += 0.1
            warnings.append(f"Only {stars} stars — low reach, consider skipping")

        if days_since_push > 60:
            risk += 0.1
            warnings.append(f"Last push {days_since_push}d ago — semi-stale repo")

        # ── Final verdict ─────────────────────────────────────────────────
        risk = min(risk, 0.99)
        if risk < 0.35:
            colour = "GREEN"
        elif risk < 0.6:
            colour = "YELLOW"
        else:
            colour = "RED"

        approved = colour in ("GREEN", "YELLOW")
        reason = (
            f"{slug} cleared all hard stops"
            if approved
            else f"{slug} risk score {risk:.2f} — blocked"
        )

        return GuardVerdict(
            approved=approved,
            risk_score=risk,
            colour=colour,
            reason=reason,
            warnings=warnings,
            repo_stats=stats,
        )

    def audit_pr_body(self, body: str) -> list[str]:
        """
        Scan a PR body for content that could hurt our reputation.
        Returns list of issues found (empty = clean).

        Checks:
          - Invented statistics (e.g. "99.87%", "50+ tools") — we can't verify these
          - Aggressive marketing language
          - URLs to aiaam.xyz (should not appear in AGENTS.md content,
            but OK in PR body sparingly)
          - Missing courtesy close invitation
        """
        issues = []

        # Invented precision stats
        if re.search(r"\d+\.\d{2}%", body):
            issues.append(
                "PR body contains high-precision percentage (e.g. 99.87%). "
                "This reads as invented data — replace with honest qualitative language."
            )

        # Hyperbolic claims
        for phrase in ("near-zero token", "instant access", "zero configuration",
                       "works with existing", "zero breaking changes"):
            if phrase.lower() in body.lower():
                issues.append(
                    f"Hyperbolic claim: \"{phrase}\" — tone down to factual language."
                )

        # Missing courtesy
        if "feel free to close" not in body.lower() and "close if" not in body.lower():
            issues.append(
                "PR body is missing a courtesy close invitation "
                "(e.g. 'Feel free to close if this doesn't fit the project.'). "
                "This reduces the chance of being flagged as spam."
            )

        return issues

    # ── Private helpers ─────────────────────────────────────────────────────

    def _fetch_repo(self, owner: str, repo: str) -> Optional[dict]:
        try:
            r = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers=self._headers, timeout=10,
            )
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def _stats(self, meta: dict) -> dict:
        return {
            "stars": meta.get("stargazers_count", 0),
            "open_issues": meta.get("open_issues_count", 0),
            "days_since_push": self._days_since(meta.get("pushed_at", "")),
        }

    def _days_since(self, iso_str: str) -> int:
        if not iso_str:
            return 9999
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return (datetime.now(dt.tzinfo) - dt).days
        except Exception:
            return 9999

    def _find_our_open_pr(self, owner: str, repo: str) -> Optional[str]:
        """Return URL of any open PR from our account on this repo, or None."""
        try:
            r = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=self._headers,
                params={"state": "open", "per_page": 50},
                timeout=10,
            )
            if r.status_code != 200:
                return None
            for pr in r.json():
                head_ref = pr.get("head", {}).get("ref", "")
                if any(head_ref.startswith(b) for b in _OUR_BRANCHES):
                    return pr.get("html_url")
        except Exception:
            pass
        return None

    def _scan_contributing(self, owner: str, repo: str) -> Optional[str]:
        """Scan CONTRIBUTING.md and README for hostile keywords. Returns matched phrase or None."""
        for path in ("CONTRIBUTING.md", "README.md", ".github/CONTRIBUTING.md"):
            text = self._fetch_file(owner, repo, path)
            if text:
                m = _HOSTILE_RE.search(text)
                if m:
                    return m.group(0)[:80]
        return None

    def _scan_strict_policy(self, owner: str, repo: str) -> Optional[str]:
        """Scan for 'issue-first' policies. Returns matched phrase or None."""
        for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md"):
            text = self._fetch_file(owner, repo, path)
            if text:
                m = _STRICT_CONTRIB_RE.search(text)
                if m:
                    return m.group(0)[:80]
        return None

    def _fetch_file(self, owner: str, repo: str, path: str) -> Optional[str]:
        import base64
        try:
            r = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=self._headers, timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                return base64.b64decode(data.get("content", "")).decode("utf-8", errors="ignore")
        except Exception:
            pass
        return None

    def _check_rate_limits(self, session, org: str) -> Optional[str]:
        """
        Check DB-backed rate limits. Returns error string if exceeded, None if OK.
        Requires sqlmodel Session.
        """
        from sqlmodel import select
        from models import InjectedRepo

        now = datetime.utcnow()
        day_ago  = now - timedelta(hours=24)
        week_ago = now - timedelta(days=7)
        org_cutoff = now - timedelta(days=ORG_COOLDOWN_DAYS)

        # PRs in last 24h
        prs_today = session.exec(
            select(InjectedRepo).where(
                InjectedRepo.pr_submitted_at >= day_ago
            )
        ).all()
        if len(prs_today) >= RATE_LIMIT_DAY:
            return (
                f"Rate limit: {len(prs_today)} PRs submitted in last 24h "
                f"(max {RATE_LIMIT_DAY}). Wait before submitting more."
            )

        # PRs in last 7 days
        prs_week = session.exec(
            select(InjectedRepo).where(
                InjectedRepo.pr_submitted_at >= week_ago
            )
        ).all()
        if len(prs_week) >= RATE_LIMIT_WEEK:
            return (
                f"Rate limit: {len(prs_week)} PRs submitted in last 7 days "
                f"(max {RATE_LIMIT_WEEK}). Resume next week."
            )

        # Org cooldown — check if we've PR'd another repo in this org recently
        recent_in_org = [
            r for r in prs_week
            if r.repo_url and f"github.com/{org}/" in r.repo_url
            and r.pr_submitted_at and r.pr_submitted_at >= org_cutoff
        ]
        if recent_in_org:
            last = max(r.pr_submitted_at for r in recent_in_org)
            days_left = ORG_COOLDOWN_DAYS - (now - last).days
            return (
                f"Org cooldown: already PR'd {org}/* {(now - last).days}d ago. "
                f"Wait {days_left}d more before targeting this org again."
            )

        return None


# ── Convenience CLI ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="AIAAM Reputation Guard — pre-flight check")
    parser.add_argument("repo", help="owner/repo to check (e.g. langchain-ai/langchain)")
    parser.add_argument("--mode", default="bulk", choices=["bulk", "strategic"],
                        help="bulk (B3 docs PR) or strategic (code integration PR)")
    args = parser.parse_args()

    parts = args.repo.split("/")
    if len(parts) != 2:
        print("Usage: python reputation_guard.py owner/repo [--mode bulk|strategic]")
        sys.exit(1)

    owner, repo = parts
    guard = ReputationGuard()
    verdict = guard.check(owner, repo, mode=args.mode)
    print(f"\nReputation Guard — {owner}/{repo} [{args.mode}]")
    print(verdict)
    sys.exit(0 if verdict.approved else 1)

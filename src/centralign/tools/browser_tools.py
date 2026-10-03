"""Browser surface: real computer use against a real web app, via Playwright.

Three decisions worth defending:

**Pages are observed as a structured summary, not as HTML.** Feeding raw DOM to
a model is expensive and, on a cheap model, actively harmful -- the signal is
buried in markup. :func:`summarize_page` extracts the things that actually drive
decisions: alert banners, headings, definition lists, tables, form fields,
buttons and links. One ERP page becomes a few hundred tokens instead of tens of
thousands, which is what makes a free-tier model viable here.

**Credentials never reach the model.** ``erp_login`` takes an *account role*
and resolves the secret from a vault file. The model can ask to act as the
finance controller -- and policy can refuse -- but it cannot read, type or
exfiltrate a password, and a prompt injection in a vendor PDF cannot either.

**Every action screenshots itself.** Evidence has to be a side effect of doing
the work. Collected at the end, it would only ever be a reconstruction.

Known limitation, stated plainly: gating a *generic* click is inherently
imprecise, because the page decides what a click does. We escalate on policy
-configured action words and rely on the ERP's own server-side authority check
as the second layer. A production deployment should prefer named connector
actions, whose effect and risk are known in advance, for anything mutating.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..runtime.state import Observation, RiskTier
from .base import FailureKind, Tool, ToolContext, ToolFailure

#: Extracts the decision-relevant structure of a page in one round trip.
_EXTRACT_JS = """
() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };

  const banners = [...document.querySelectorAll('[role=alert],[role=status],.banner')]
    .filter(visible).map((el) => ({
      kind: el.classList.contains('error') || el.getAttribute('role') === 'alert' ? 'error' : 'notice',
      text: clean(el.innerText),
    })).filter((b) => b.text);

  const headings = [...document.querySelectorAll('h1,h2,h3')]
    .filter(visible).map((el) => clean(el.innerText)).filter(Boolean);

  // Page chrome: usually carries who is signed in and with what role. The agent
  // must be able to see its own identity to know whether a session switch took
  // effect, so this is decision-relevant, not decoration.
  const chrome = [...document.querySelectorAll('header,[role=banner]')]
    .filter(visible).map((el) => clean(el.innerText)).filter(Boolean);

  const labelFor = (el) => {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    if (el.placeholder) return clean(el.placeholder);
    return el.name || el.id || '';
  };

  const fields = [...document.querySelectorAll('input,textarea,select')]
    .filter((el) => visible(el) && el.type !== 'hidden')
    .map((el) => ({
      label: labelFor(el),
      name: el.name || '',
      id: el.id || '',
      type: el.type || el.tagName.toLowerCase(),
      value: el.type === 'password' ? '' : clean(el.value).slice(0, 80),
      required: !!el.required,
    }));

  const buttons = [...document.querySelectorAll('button,input[type=submit]')]
    .filter(visible)
    .map((el) => clean(el.innerText || el.value))
    .filter(Boolean);

  const links = [...document.querySelectorAll('a[href]')]
    .filter(visible)
    .map((el) => ({ text: clean(el.innerText), href: el.getAttribute('href') }))
    .filter((l) => l.text);

  const definitions = [];
  document.querySelectorAll('dl').forEach((dl) => {
    const kids = [...dl.children];
    for (let i = 0; i < kids.length; i += 1) {
      if (kids[i].tagName === 'DT') {
        const dd = kids[i + 1] && kids[i + 1].tagName === 'DD' ? kids[i + 1] : null;
        definitions.push({ term: clean(kids[i].innerText), value: dd ? clean(dd.innerText) : '' });
      }
    }
  });

  const tables = [...document.querySelectorAll('table')].filter(visible).map((t) => ({
    headers: [...t.querySelectorAll('thead th')].map((th) => clean(th.innerText)),
    rows: [...t.querySelectorAll('tbody tr')]
      .slice(0, 40)
      .map((tr) => [...tr.children].map((td) => clean(td.innerText))),
  }));

  return {
    title: clean(document.title),
    url: location.href,
    chrome, banners, headings, fields, buttons, links, definitions, tables,
  };
}
"""


def render_page(snapshot: dict[str, Any]) -> str:
    """Format a snapshot as the compact text the model reads."""
    lines = [f"PAGE: {snapshot.get('title', '')}", f"URL: {snapshot.get('url', '')}"]

    chrome = snapshot.get("chrome") or []
    if chrome:
        lines.append("SIGNED IN AS / PAGE CHROME: " + " ".join(chrome)[:300])

    for banner in snapshot.get("banners", []):
        lines.append(f"[{banner['kind'].upper()}] {banner['text']}")

    headings = snapshot.get("headings") or []
    if headings:
        lines.append("HEADINGS: " + " | ".join(headings[:8]))

    definitions = snapshot.get("definitions") or []
    if definitions:
        lines.append("DETAILS:")
        lines += [f"  {d['term']}: {d['value']}" for d in definitions[:30]]

    for index, table in enumerate(snapshot.get("tables") or [], start=1):
        if not table.get("rows"):
            continue
        lines.append(f"TABLE {index}: " + " | ".join(table.get("headers") or []))
        for row in table["rows"][:25]:
            lines.append("  " + " | ".join(row))

    fields = snapshot.get("fields") or []
    if fields:
        lines.append("FORM FIELDS (fill by label or name):")
        for field in fields[:25]:
            marker = " *required" if field.get("required") else ""
            current = f" current={field['value']!r}" if field.get("value") else ""
            lines.append(
                f"  label={field['label']!r} name={field['name']!r} "
                f"type={field['type']}{marker}{current}"
            )

    buttons = snapshot.get("buttons") or []
    if buttons:
        lines.append("BUTTONS: " + " | ".join(dict.fromkeys(buttons))[:600])

    links = snapshot.get("links") or []
    if links:
        seen: dict[str, str] = {}
        for link in links:
            seen.setdefault(link["text"], link["href"])
        lines.append(
            "LINKS: " + " | ".join(f"{text} -> {href}" for text, href in list(seen.items())[:25])
        )

    return "\n".join(lines)


class BrowserSession:
    """One Chromium context per run, so the ERP session cookie survives steps."""

    def __init__(self, *, headless: bool, ctx: ToolContext) -> None:
        self.headless = headless
        self.ctx = ctx
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._shots = 0
        self.account: str | None = None
        #: (url, status) of the most recent main-frame document response.
        #: Without this, a form submit that returns 503 looks identical to one
        #: that succeeded -- the click resolves, the page changes, and the tool
        #: would report success while nothing was saved. That is the single
        #: most dangerous way browser automation lies.
        self.last_document: tuple[str, int] | None = None

    async def page(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise ToolFailure(
                "playwright is not installed", kind=FailureKind.UNAVAILABLE
            ) from exc

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure(
                f"could not launch Chromium: {exc}",
                kind=FailureKind.UNAVAILABLE,
                hint="run 'python -m playwright install chromium'",
            ) from exc
        self._context = await self._browser.new_context(viewport={"width": 1280, "height": 900})
        self._context.set_default_timeout(15000)
        self._page = await self._context.new_page()
        self._page.on("response", self._record_document_response)
        return self._page

    def _record_document_response(self, response) -> None:
        try:
            request = response.request
            if request.resource_type == "document" and request.is_navigation_request():
                self.last_document = (response.url, response.status)
        except Exception:  # noqa: BLE001 - an observer must never break a run
            pass

    async def snapshot(self, label: str) -> tuple[str, str]:
        """Return (rendered page text, relative screenshot path)."""
        page = await self.page()
        raw = await page.evaluate(_EXTRACT_JS)
        self._shots += 1
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]
        shot = self.ctx.artifact_path(f"shot-{self._shots:03d}-{safe}.png")
        try:
            await page.screenshot(path=str(shot), full_page=True)
        except Exception:  # noqa: BLE001 - evidence is best-effort, never fatal
            return render_page(raw), ""
        return render_page(raw), self.ctx.relative_artifact(shot)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # noqa: BLE001
                    pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        self._playwright = self._browser = self._context = self._page = None


async def _session(ctx: ToolContext) -> BrowserSession:
    session = ctx.resources.get("browser")
    if session is None:
        session = BrowserSession(headless=ctx.settings.headless, ctx=ctx)
        ctx.resources["browser"] = session
    return session


async def _resolve(page, target: str):
    """Find one element from a human description, trying the likely readings.

    Order matters: role-based lookups first, because a model naming "Approve
    invoice" means the button, and a bare text match could hit a heading with
    the same words.
    """
    target = str(target).strip()
    candidates = []
    if target.startswith(("#", ".", "[")) or ">" in target:
        candidates.append(page.locator(target))
    candidates += [
        page.get_by_role("button", name=target, exact=False),
        page.get_by_role("link", name=target, exact=False),
        page.get_by_label(target, exact=False),
        page.get_by_placeholder(target, exact=False),
        page.locator(f"[name={target!r}]"),
        page.locator(f"#{target}"),
        page.get_by_text(target, exact=False),
    ]
    for locator in candidates:
        try:
            if await locator.count() > 0:
                return locator.first
        except Exception:  # noqa: BLE001 - an invalid selector is just a miss
            continue
    return None


async def _available_targets(page) -> str:
    try:
        raw = await page.evaluate(_EXTRACT_JS)
    except Exception:  # noqa: BLE001
        return "(could not inspect the page)"
    buttons = ", ".join(dict.fromkeys(raw.get("buttons") or [])) or "(none)"
    fields = ", ".join(
        f"{f['label'] or f['name']}" for f in (raw.get("fields") or [])
    ) or "(none)"
    links = ", ".join(dict.fromkeys(link["text"] for link in (raw.get("links") or [])))[:300]
    return f"buttons: {buttons}; fields: {fields}; links: {links}"


# ---------------------------------------------------------------------------
# credential vault
# ---------------------------------------------------------------------------


def load_vault(company_dir: Path) -> dict[str, Any]:
    path = company_dir / "credentials.sandbox.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


class ErpLogin(Tool):
    name = "erp_login"
    surface = "erp"
    description = (
        "Sign in to Nexus ERP as a named account role. Credentials are held in a "
        "vault and are never exposed; you choose a role, not a password. "
        "Roles: 'ap_clerk' (approval limit INR 50,000) and 'finance_controller' "
        "(higher authority, requires approval to use). Call this before any other "
        "ERP action, and again to switch roles."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "account": {
                "type": "string",
                "enum": ["ap_clerk", "finance_controller"],
                "description": "Which account role to sign in as.",
            }
        },
        "required": ["account"],
    }
    risk = RiskTier.READ

    def risk_for(self, args: dict[str, Any]) -> RiskTier:
        # Assuming a higher-authority identity is itself a privileged act.
        if str(args.get("account", "")).strip() == "finance_controller":
            return RiskTier.SENSITIVE
        return RiskTier.READ

    def preview(self, args: dict[str, Any]) -> str:
        return f"Sign in to Nexus ERP using the '{args.get('account')}' service account"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        account = str(args["account"]).strip()
        vault = load_vault(ctx.settings.company_dir).get("nexus_erp", {})
        credential = vault.get(account)
        if not credential:
            raise ToolFailure(
                f"no credential configured for account role {account!r}",
                kind=FailureKind.PERMISSION,
                hint=f"configured roles: {', '.join(vault) or '(none)'}",
            )

        session = await _session(ctx)
        page = await session.page()
        await page.goto(f"{ctx.settings.erp_url}/logout", wait_until="domcontentloaded")
        await page.goto(f"{ctx.settings.erp_url}/", wait_until="domcontentloaded")
        await page.fill("#username", credential["username"])
        await page.fill("#password", credential["password"])
        await page.click("button[type=submit]")
        await page.wait_for_load_state("domcontentloaded")

        rendered, shot = await session.snapshot(f"login-{account}")
        if "/dashboard" not in page.url:
            raise ToolFailure(
                f"sign-in as {account} did not reach the dashboard",
                kind=FailureKind.PERMISSION,
                data={"page": rendered},
                hint="the credential may be wrong or the ERP may be unreachable",
            )
        session.account = account
        return Observation(
            ok=True,
            summary=f"Signed in to Nexus ERP as '{account}'.\n{rendered}",
            data={"account": account, "url": page.url},
            artifacts=[shot] if shot else [],
        )


class BrowserOpen(Tool):
    name = "browser_open"
    surface = "erp"
    description = (
        "Open a page in Nexus ERP and return its contents as a structured summary. "
        "Accepts a path such as '/purchase-orders/PO-4471' or '/invoices?q=INV-HL-2207'. "
        "A 404 page is returned as an observation, not an error."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path within Nexus ERP, starting with '/'.",
            }
        },
        "required": ["path"],
    }
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        session = await _session(ctx)
        page = await session.page()
        path = str(args["path"]).strip()
        url = path if path.startswith("http") else f"{ctx.settings.erp_url}/{path.lstrip('/')}"

        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure(
                f"could not load {url}: {exc}",
                kind=FailureKind.UNAVAILABLE,
                hint="is the sandbox running? 'python -m sandbox.serve'",
            ) from exc

        status = response.status if response else 0
        if status >= 500:
            raise ToolFailure(
                f"Nexus ERP returned HTTP {status} for {path}",
                kind=FailureKind.TRANSIENT,
                hint="transient server error; retrying is appropriate",
            )

        rendered, shot = await session.snapshot(f"open-{path}")
        if "Sign in to Nexus ERP" in rendered:
            raise ToolFailure(
                "not signed in to Nexus ERP (redirected to the sign-in page)",
                kind=FailureKind.PRECONDITION,
                hint="call erp_login first",
            )

        return Observation(
            ok=True,
            summary=rendered,
            data={"url": page.url, "http_status": status, "page": rendered},
            artifacts=[shot] if shot else [],
        )


class BrowserRead(Tool):
    name = "browser_read"
    surface = "erp"
    description = (
        "Re-read the page currently open in the browser without navigating. Use this "
        "to confirm the state of a record after an action, or to verify an outcome."
    )
    args_schema = {"type": "object", "properties": {}, "required": []}
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        session = ctx.resources.get("browser")
        if session is None:
            raise ToolFailure(
                "no page is open", kind=FailureKind.PRECONDITION, hint="use browser_open first"
            )
        rendered, shot = await session.snapshot("read")
        page = await session.page()
        return Observation(
            ok=True,
            summary=rendered,
            data={"url": page.url, "page": rendered},
            artifacts=[shot] if shot else [],
        )


class BrowserFill(Tool):
    name = "browser_fill"
    surface = "erp"
    description = (
        "Type values into fields of the form on the current page. Keys are field "
        "labels or names exactly as reported in FORM FIELDS. This fills only; it "
        "does not submit."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "description": "Map of field label or name to the value to type.",
            }
        },
        "required": ["fields"],
    }
    risk = RiskTier.READ  # typing is not a mutation; submitting is

    def preview(self, args: dict[str, Any]) -> str:
        fields = args.get("fields") or {}
        return "Fill form: " + ", ".join(f"{k}={v}" for k, v in list(fields.items())[:6])

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        session = ctx.resources.get("browser")
        if session is None:
            raise ToolFailure(
                "no page is open", kind=FailureKind.PRECONDITION, hint="use browser_open first"
            )
        page = await session.page()
        fields = args.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            raise ToolFailure("'fields' must be a non-empty object", kind=FailureKind.INVALID_ARGS)

        filled: list[str] = []
        for key, value in fields.items():
            locator = await _resolve(page, key)
            if locator is None:
                raise ToolFailure(
                    f"no form field matching {key!r} on this page",
                    kind=FailureKind.NOT_FOUND,
                    data={"filled_before_failure": filled},
                    hint=f"available -- {await _available_targets(page)}",
                )
            try:
                await locator.fill(str(value))
            except Exception as exc:  # noqa: BLE001
                raise ToolFailure(
                    f"could not type into {key!r}: {exc}",
                    kind=FailureKind.INVALID_ARGS,
                    data={"filled_before_failure": filled},
                ) from exc
            filled.append(key)

        rendered, shot = await session.snapshot("fill")
        return Observation(
            ok=True,
            summary=f"Filled {len(filled)} field(s): {', '.join(filled)}.\n{rendered}",
            data={"filled": filled, "page": rendered},
            artifacts=[shot] if shot else [],
        )


class BrowserClick(Tool):
    name = "browser_click"
    surface = "erp"
    description = (
        "Click a button or link by its visible text, then return the resulting page. "
        "Use this to submit a form or to follow a link. The resulting page may carry "
        "an [ERROR] banner -- that is the system refusing the action, and it is "
        "information, not a crash."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Visible text of the button or link, e.g. 'Approve invoice'.",
            }
        },
        "required": ["target"],
    }
    risk = RiskTier.WRITE
    idempotent = False

    #: Action words that mark a click as a consequential mutation. Overridable
    #: from policy data so a new deployment does not need a code change.
    HIGH_RISK_WORDS = ("approve", "pay", "post", "delete", "submit payment", "authorise", "authorize")

    def risk_for(self, args: dict[str, Any]) -> RiskTier:
        target = str(args.get("target", "")).lower()
        if any(word in target for word in self.HIGH_RISK_WORDS):
            return RiskTier.SENSITIVE
        return RiskTier.WRITE

    def preview(self, args: dict[str, Any]) -> str:
        return f"Click '{args.get('target')}' in Nexus ERP"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        session = ctx.resources.get("browser")
        if session is None:
            raise ToolFailure(
                "no page is open", kind=FailureKind.PRECONDITION, hint="use browser_open first"
            )
        page = await session.page()
        target = str(args["target"])

        locator = await _resolve(page, target)
        if locator is None:
            raise ToolFailure(
                f"nothing matching {target!r} to click on this page",
                kind=FailureKind.NOT_FOUND,
                hint=f"available -- {await _available_targets(page)}",
            )

        origin = page.url
        session.last_document = None
        try:
            await locator.click()
            await page.wait_for_load_state("domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure(
                f"click on {target!r} failed: {exc}",
                kind=FailureKind.TRANSIENT,
                hint="the element may have moved; re-read the page and try again",
            ) from exc

        # A server error after a submit means the action did NOT take effect.
        # Restore the page we came from so the form can be re-driven, then report
        # a retryable failure. Returning ok here would record a phantom success.
        document = session.last_document
        if document and document[1] >= 500:
            try:
                await page.goto(origin, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
            raise ToolFailure(
                f"clicking {target!r} returned HTTP {document[1]} -- the action did not take effect",
                kind=FailureKind.TRANSIENT,
                hint=(
                    "the submission never reached the server, so nothing was saved. "
                    "Re-open the form, re-enter the values and submit again."
                ),
                data={"http_status": document[1], "restored_url": origin},
            )

        rendered, shot = await session.snapshot(f"click-{target}")
        # A refusal banner is a legitimate observation. Returning ok=False here
        # would make the kernel treat the ERP's considered "no" as a transport
        # fault and retry it, which is exactly the wrong response.
        has_error = "[ERROR]" in rendered
        return Observation(
            ok=True,
            summary=f"Clicked {target!r}.\n{rendered}",
            data={
                "clicked": target,
                "url": page.url,
                "page": rendered,
                "refused": has_error,
            },
            artifacts=[shot] if shot else [],
        )


BROWSER_TOOLS = [ErpLogin(), BrowserOpen(), BrowserRead(), BrowserFill(), BrowserClick()]

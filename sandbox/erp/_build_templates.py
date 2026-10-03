"""One-shot generator for the ERP Jinja templates.

Kept in the repo so the sandbox UI can be regenerated or diffed as a unit
rather than hand-edited across ten files.
"""

from pathlib import Path

OUT = Path(__file__).parent / "templates"

BASE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Nexus ERP{% endblock %}</title>
<style>
  :root { --line:#d8dde5; --ink:#1b2330; --muted:#5c6b80; --accent:#1f4e9c; --bg:#f4f6f9; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 "Segoe UI",system-ui,sans-serif; color:var(--ink); background:var(--bg); }
  header { background:#132943; color:#fff; padding:10px 20px; display:flex; align-items:center; gap:24px; }
  header .brand { font-weight:700; letter-spacing:.4px; }
  header nav a { color:#cdd9ea; text-decoration:none; margin-right:16px; }
  header nav a:hover { color:#fff; text-decoration:underline; }
  header .who { margin-left:auto; font-size:13px; color:#aebfd6; }
  main { max-width:1040px; margin:24px auto; padding:0 20px 60px; }
  h1 { font-size:20px; margin:0 0 4px; }
  h2 { font-size:15px; margin:24px 0 8px; color:var(--muted); text-transform:uppercase; letter-spacing:.6px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:6px; padding:18px; margin-bottom:18px; }
  table { width:100%; border-collapse:collapse; background:#fff; }
  th,td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); }
  th { background:#eef2f7; font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:var(--muted); }
  td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; }
  a { color:var(--accent); }
  .banner { padding:11px 14px; border-radius:5px; margin-bottom:16px; border:1px solid; }
  .banner.error { background:#fdeaea; border-color:#e4a0a0; color:#8a1f1f; }
  .banner.ok { background:#e9f6ec; border-color:#9fcfae; color:#1d6130; }
  .pill { display:inline-block; padding:2px 9px; border-radius:99px; font-size:12px; font-weight:600; }
  .pill.registered { background:#e6effa; color:#1f4e9c; }
  .pill.approved { background:#e3f5e8; color:#1d6130; }
  .pill.on_hold { background:#fdf0e0; color:#8a5a12; }
  label { display:block; font-size:12px; font-weight:600; color:var(--muted); margin:12px 0 4px; }
  input[type=text],input[type=password],textarea { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:4px; font:inherit; }
  button { background:var(--accent); color:#fff; border:0; border-radius:4px; padding:9px 18px; font:inherit; font-weight:600; cursor:pointer; }
  button.secondary { background:#6b7585; }
  .row { display:flex; gap:12px; align-items:flex-end; }
  .stat { display:inline-block; min-width:150px; }
  .stat b { display:block; font-size:26px; }
  dl.kv { display:grid; grid-template-columns:230px 1fr; gap:6px 16px; margin:0; }
  dl.kv dt { color:var(--muted); font-size:13px; }
  dl.kv dd { margin:0; font-weight:600; }
  form.inline { display:inline; }
</style>
</head>
<body>
<header>
  <span class="brand">NEXUS&nbsp;ERP</span>
  {% if user %}
  <nav>
    <a href="/dashboard">Dashboard</a>
    <a href="/purchase-orders">Purchase Orders</a>
    <a href="/invoices">Invoices</a>
    <a href="/vendors">Vendors</a>
  </nav>
  <span class="who">{{ user.name }} &middot; {{ user.role }} &middot; <a href="/logout" style="color:#cdd9ea">Sign out</a></span>
  {% endif %}
</header>
<main>
  {% if error %}<div class="banner error" role="alert">{{ error }}</div>{% endif %}
  {% if notice %}<div class="banner ok" role="status">{{ notice }}</div>{% endif %}
  {% block body %}{% endblock %}
</main>
</body>
</html>
"""

LOGIN = """{% extends "base.html" %}
{% block title %}Sign in - Nexus ERP{% endblock %}
{% block body %}
<h1>Sign in to Nexus ERP</h1>
<p style="color:var(--muted)">Finance and procurement system of record.</p>
<div class="card" style="max-width:380px">
  <form method="post" action="/login">
    <label for="username">Username</label>
    <input type="text" id="username" name="username" autocomplete="username" required>
    <label for="password">Password</label>
    <input type="password" id="password" name="password" autocomplete="current-password" required>
    <div style="margin-top:18px"><button type="submit">Sign in</button></div>
  </form>
</div>
{% endblock %}
"""

DASHBOARD = """{% extends "base.html" %}
{% block title %}Dashboard - Nexus ERP{% endblock %}
{% block body %}
<h1>Dashboard</h1>
<div class="card">
  <span class="stat"><b>{{ counts.open_pos }}</b>Open purchase orders</span>
  <span class="stat"><b>{{ counts.registered }}</b>Invoices registered</span>
  <span class="stat"><b>{{ counts.approved }}</b>Invoices approved</span>
  <span class="stat"><b>{{ counts.on_hold }}</b>Invoices on hold</span>
</div>
<h2>Quick actions</h2>
<div class="card">
  <a href="/invoices/new">Register a new invoice</a> &nbsp;|&nbsp;
  <a href="/purchase-orders">Browse purchase orders</a> &nbsp;|&nbsp;
  <a href="/invoices">Review invoices</a>
</div>
{% endblock %}
"""

PURCHASE_ORDERS = """{% extends "base.html" %}
{% block title %}Purchase Orders - Nexus ERP{% endblock %}
{% block body %}
<h1>Purchase Orders</h1>
<div class="card">
  <form method="get" action="/purchase-orders" class="row">
    <div style="flex:1"><label for="q">Search by PO number or vendor</label>
    <input type="text" id="q" name="q" value="{{ q }}"></div>
    <button type="submit">Search</button>
  </form>
</div>
<table>
  <thead><tr><th>PO Number</th><th>Vendor</th><th>Status</th><th class="num">Ordered</th><th class="num">Billed</th><th class="num">Remaining</th></tr></thead>
  <tbody>
  {% for po in rows %}
    <tr>
      <td><a href="/purchase-orders/{{ po.number }}">{{ po.number }}</a></td>
      <td>{{ po.vendor }}</td>
      <td>{{ po.status }}</td>
      <td class="num">{{ po.total|inr }}</td>
      <td class="num">{{ po.billed|inr }}</td>
      <td class="num">{{ po.remaining|inr }}</td>
    </tr>
  {% else %}
    <tr><td colspan="6">No purchase orders matched.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

PO_DETAIL = """{% extends "base.html" %}
{% block title %}{{ po.number }} - Nexus ERP{% endblock %}
{% block body %}
<h1>Purchase Order {{ po.number }}</h1>
<div class="card">
  <dl class="kv">
    <dt>Vendor</dt><dd>{{ po.vendor }}</dd>
    <dt>Status</dt><dd>{{ po.status }}</dd>
    <dt>Category</dt><dd>{{ po.category }}</dd>
    <dt>Ordered value</dt><dd>{{ po.currency }} {{ po.total|inr }}</dd>
    <dt>Already billed</dt><dd>{{ po.currency }} {{ po.billed|inr }}</dd>
    <dt>Remaining balance</dt><dd id="remaining-balance">{{ po.currency }} {{ remaining|inr }}</dd>
  </dl>
</div>
<h2>Order lines</h2>
<table>
  <thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Line total</th></tr></thead>
  <tbody>
  {% for line in po.lines %}
    <tr><td>{{ line.description }}</td><td class="num">{{ line.qty }}</td><td class="num">{{ line.rate|inr }}</td><td class="num">{{ (line.qty * line.rate)|inr }}</td></tr>
  {% endfor %}
  </tbody>
</table>
<h2>Invoices against this PO</h2>
<table>
  <thead><tr><th>Invoice</th><th>Status</th><th class="num">Amount</th></tr></thead>
  <tbody>
  {% for inv in invoices %}
    <tr><td><a href="/invoices/{{ inv.id }}">{{ inv.number }}</a></td><td><span class="pill {{ inv.status }}">{{ inv.status }}</span></td><td class="num">{{ inv.amount|inr }}</td></tr>
  {% else %}
    <tr><td colspan="3">No invoices registered against this purchase order.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

VENDORS = """{% extends "base.html" %}
{% block title %}Vendors - Nexus ERP{% endblock %}
{% block body %}
<h1>Approved Vendor Master</h1>
<p style="color:var(--muted)">Only vendors listed here are approved for payment.</p>
<div class="card">
  <form method="get" action="/vendors" class="row">
    <div style="flex:1"><label for="q">Search vendor name</label><input type="text" id="q" name="q" value="{{ q }}"></div>
    <button type="submit">Search</button>
  </form>
</div>
<table>
  <thead><tr><th>Vendor ID</th><th>Name</th><th>Approved</th><th>Payment terms</th><th>GSTIN</th></tr></thead>
  <tbody>
  {% for v in rows %}
    <tr><td>{{ v.id }}</td><td>{{ v.name }}</td><td>{{ "yes" if v.approved else "no" }}</td><td>{{ v.payment_terms }}</td><td>{{ v.tax_id }}</td></tr>
  {% else %}
    <tr><td colspan="5">No vendor in the approved master matched this search.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

INVOICES = """{% extends "base.html" %}
{% block title %}Invoices - Nexus ERP{% endblock %}
{% block body %}
<h1>Invoices</h1>
<div class="card">
  <form method="get" action="/invoices" class="row">
    <div style="flex:1"><label for="q">Search by invoice number or vendor</label><input type="text" id="q" name="q" value="{{ q }}"></div>
    <button type="submit">Search</button>
  </form>
  <p style="margin:14px 0 0"><a href="/invoices/new">Register a new invoice</a></p>
</div>
<table>
  <thead><tr><th>Invoice</th><th>Vendor</th><th>PO</th><th>Status</th><th class="num">Amount</th><th>Approved by</th></tr></thead>
  <tbody>
  {% for inv in rows %}
    <tr>
      <td><a href="/invoices/{{ inv.id }}">{{ inv.number }}</a></td>
      <td>{{ inv.vendor }}</td><td>{{ inv.po_number }}</td>
      <td><span class="pill {{ inv.status }}">{{ inv.status }}</span></td>
      <td class="num">{{ inv.amount|inr }}</td>
      <td>{{ inv.approved_by or "-" }}</td>
    </tr>
  {% else %}
    <tr><td colspan="6">No invoices matched.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

INVOICE_NEW = """{% extends "base.html" %}
{% block title %}Register invoice - Nexus ERP{% endblock %}
{% block body %}
<h1>Register a new invoice</h1>
<p style="color:var(--muted)">The invoice must reference an existing purchase order.</p>
<div class="card" style="max-width:560px">
  <form method="post" action="/invoices">
    <label for="number">Invoice number</label>
    <input type="text" id="number" name="number" required>
    <label for="vendor">Vendor name</label>
    <input type="text" id="vendor" name="vendor" required>
    <label for="po_number">Purchase order number</label>
    <input type="text" id="po_number" name="po_number" required>
    <label for="amount">Invoice amount</label>
    <input type="text" id="amount" name="amount" required>
    <label for="invoice_date">Invoice date</label>
    <input type="text" id="invoice_date" name="invoice_date" placeholder="YYYY-MM-DD">
    <label for="gl_code">GL code</label>
    <input type="text" id="gl_code" name="gl_code">
    <div style="margin-top:20px"><button type="submit">Register invoice</button></div>
  </form>
</div>
{% endblock %}
"""

INVOICE_DETAIL = """{% extends "base.html" %}
{% block title %}{{ invoice.number }} - Nexus ERP{% endblock %}
{% block body %}
<h1>Invoice {{ invoice.number }}</h1>
<div class="card">
  <dl class="kv">
    <dt>Status</dt><dd id="invoice-status"><span class="pill {{ invoice.status }}">{{ invoice.status }}</span></dd>
    <dt>Vendor</dt><dd>{{ invoice.vendor }}</dd>
    <dt>Purchase order</dt><dd>{% if po %}<a href="/purchase-orders/{{ po.number }}">{{ invoice.po_number }}</a>{% else %}{{ invoice.po_number }} (not found){% endif %}</dd>
    <dt>Amount</dt><dd id="invoice-amount">{{ invoice.currency }} {{ invoice.amount|inr }}</dd>
    {% if remaining is not none %}<dt>PO remaining before this invoice</dt><dd>{{ invoice.currency }} {{ remaining|inr }}</dd>{% endif %}
    <dt>Invoice date</dt><dd>{{ invoice.invoice_date or "-" }}</dd>
    <dt>GL code</dt><dd>{{ invoice.gl_code or "-" }}</dd>
    <dt>Registered by</dt><dd>{{ invoice.registered_by or "-" }}</dd>
    <dt>Approved by</dt><dd>{{ invoice.approved_by or "-" }}</dd>
    {% if invoice.notes %}<dt>Notes</dt><dd>{{ invoice.notes }}</dd>{% endif %}
  </dl>
</div>
<h2>Actions</h2>
<div class="card">
  <form class="inline" method="post" action="/invoices/{{ invoice.id }}/approve">
    <button type="submit">Approve invoice</button>
  </form>
  <form method="post" action="/invoices/{{ invoice.id }}/hold" style="margin-top:16px">
    <label for="reason">Reason for placing on hold</label>
    <textarea id="reason" name="reason" rows="3" required></textarea>
    <div style="margin-top:10px"><button class="secondary" type="submit">Place on hold</button></div>
  </form>
</div>
<h2>History</h2>
<table>
  <thead><tr><th>Action</th><th>By</th><th>Detail</th></tr></thead>
  <tbody>
  {% for h in invoice.history %}
    <tr><td>{{ h.action }}</td><td>{{ h.by }}</td><td>{{ h.reason or "-" }}</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
"""

NOT_FOUND = """{% extends "base.html" %}
{% block title %}Not found - Nexus ERP{% endblock %}
{% block body %}
<h1>{{ what }} {{ identifier }} not found</h1>
<div class="banner error">{{ hint }}</div>
<p><a href="/dashboard">Return to dashboard</a></p>
{% endblock %}
"""

TEMPLATES = {
    "base.html": BASE,
    "login.html": LOGIN,
    "dashboard.html": DASHBOARD,
    "purchase_orders.html": PURCHASE_ORDERS,
    "purchase_order_detail.html": PO_DETAIL,
    "vendors.html": VENDORS,
    "invoices.html": INVOICES,
    "invoice_new.html": INVOICE_NEW,
    "invoice_detail.html": INVOICE_DETAIL,
    "not_found.html": NOT_FOUND,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in TEMPLATES.items():
        (OUT / name).write_text(body, encoding="utf-8")
    print(f"wrote {len(TEMPLATES)} templates to {OUT}")


if __name__ == "__main__":
    main()

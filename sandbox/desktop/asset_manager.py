"""Asset & Access Manager -- a mock internal **desktop** application.

The third surface, and the one the problem statement named that a browser cannot
cover. This is a Qt desktop app with no web interface, which is a deliberately
realistic shape for an internal IT tool: the read side was later exposed as an
API for reporting, but every write is still GUI-only. An operator that wants to
create an account has to drive the window.

Why Qt rather than Tkinter: Qt sets real UIA ``AccessibleName`` values on its
controls, so the operator can target "Username" or "Create account" by name the
way a person reads a label. Tkinter exposes anonymous ``Edit`` boxes, which would
have forced the tool into pairing labels with controls by tab order -- workable,
but it would have made the demo about my heuristic rather than about the agent.

As with Nexus ERP, this app validates and refuses. Refusals are the interesting
part:

* an account that already exists is rejected outright;
* an asset already held by someone else names the holder;
* an asset in repair cannot be assigned;
* a **restricted** access group refuses without Finance Controller approval,
  which forces the operator to stop and ask rather than quietly skip it.

Run it with ``python -m sandbox.desktop.asset_manager``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sandbox.store import STORE  # noqa: E402

WINDOW_TITLE = "Asset and Access Manager"

#: Roles the app knows, and the access groups each is normally granted. The
#: operator should read the SOP rather than infer this, but the app enforces
#: nothing about it -- deciding what a joiner needs is the operator's job.
ROLES = [
    "QA Engineer",
    "Software Engineer",
    "Accounts Payable Clerk",
    "Finance Controller",
    "Procurement Lead",
]
DEPARTMENTS = ["Engineering", "Finance", "Procurement", "People Operations"]


def _named(widget: QWidget, name: str) -> QWidget:
    """Give a control a stable accessible name, which is what UIA exposes."""
    widget.setAccessibleName(name)
    widget.setObjectName(name.replace(" ", "_").lower())
    return widget


class AssetManager(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(980, 680)

        tabs = QTabWidget()
        _named(tabs, "Sections")
        tabs.addTab(self._directory_tab(), "Directory")
        tabs.addTab(self._assets_tab(), "Assets")
        tabs.addTab(self._access_tab(), "Access Groups")
        self.setCentralWidget(tabs)

        # A read-only Edit rather than a Label, because UIA reports a Label's
        # *name* as its text -- and the name is what we target it by. An Edit
        # keeps the two separate: stable name for targeting, live value for the
        # message. Without this, reading the status back returned the string
        # "Status message" forever.
        self.status = _named(QLineEdit("Ready."), "Status message")
        self.status.setReadOnly(True)
        self.status.setFrame(False)
        self.statusBar().addWidget(self.status, 1)

        self.refresh()
        # Pick up changes made by the API or a reset while the window is open.
        self._poll = QTimer(self)
        self._poll.timeout.connect(self.refresh)
        self._poll.start(1500)

    # -- feedback ----------------------------------------------------------
    def _say(self, message: str, *, error: bool = False) -> None:
        """Report the outcome of an action.

        Prefixed so the operator can tell a refusal from a confirmation without
        parsing prose -- the same job the ERP's coloured banner does.
        """
        self.status.setText(("ERROR: " if error else "OK: ") + message)
        self.status.setStyleSheet(
            "color:#8a1f1f;font-weight:600;background:transparent"
            if error
            else "color:#1d6130;font-weight:600;background:transparent"
        )

    # -- Directory ---------------------------------------------------------
    def _directory_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.accounts_list = _named(QListWidget(), "Accounts list")
        header = QLabel("Username | Full name | Role | Department | Status")
        _named(header, "Accounts list columns")
        header.setStyleSheet("color:#5c6b80;font-weight:600")
        layout.addWidget(header)
        layout.addWidget(self.accounts_list)

        box = QGroupBox("Create a new account")
        form = QFormLayout(box)
        self.new_full_name = _named(QLineEdit(), "Full name")
        self.new_username = _named(QLineEdit(), "Username")
        self.new_role = _named(QComboBox(), "Role")
        self.new_role.addItems(ROLES)
        self.new_department = _named(QComboBox(), "Department")
        self.new_department.addItems(DEPARTMENTS)
        self.new_start_date = _named(QLineEdit(), "Start date")
        self.new_start_date.setPlaceholderText("YYYY-MM-DD")

        form.addRow("Full name", self.new_full_name)
        form.addRow("Username", self.new_username)
        form.addRow("Role", self.new_role)
        form.addRow("Department", self.new_department)
        form.addRow("Start date", self.new_start_date)

        create = _named(QPushButton("Create account"), "Create account")
        create.clicked.connect(self.create_account)
        form.addRow("", create)
        layout.addWidget(box)
        return page

    def create_account(self) -> None:
        username = self.new_username.text().strip()
        full_name = self.new_full_name.text().strip()
        start_date = self.new_start_date.text().strip()

        if not username or not full_name:
            self._say("Full name and Username are both required.", error=True)
            return
        if " " in username:
            self._say(f"Username '{username}' must not contain spaces.", error=True)
            return

        state = STORE.load()
        if any(a["username"].lower() == username.lower() for a in state["accounts"]):
            existing = next(a for a in state["accounts"] if a["username"].lower() == username.lower())
            self._say(
                f"An account '{username}' already exists for {existing['full_name']} "
                f"({existing['role']}). Duplicate accounts are not permitted.",
                error=True,
            )
            return

        record = {
            "username": username,
            "full_name": full_name,
            "role": self.new_role.currentText(),
            "department": self.new_department.currentText(),
            "status": "active",
            "start_date": start_date,
        }

        def apply(st: dict[str, Any]) -> None:
            st["accounts"].append(record)
            st.setdefault("audit", []).append({"action": "account.created", "username": username})

        STORE.mutate(apply)
        self._say(f"Account '{username}' created for {full_name} as {record['role']}.")
        for field in (self.new_full_name, self.new_username, self.new_start_date):
            field.clear()
        self.refresh()

    # -- Assets ------------------------------------------------------------
    def _assets_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.assets_list = _named(QListWidget(), "Assets list")
        header = QLabel("Asset tag | Type | Model | Status | Assigned to")
        _named(header, "Assets list columns")
        header.setStyleSheet("color:#5c6b80;font-weight:600")
        layout.addWidget(header)
        layout.addWidget(self.assets_list)

        box = QGroupBox("Assign an asset")
        form = QFormLayout(box)
        self.assign_tag = _named(QLineEdit(), "Asset tag")
        self.assign_user = _named(QLineEdit(), "Assign to username")
        form.addRow("Asset tag", self.assign_tag)
        form.addRow("Assign to username", self.assign_user)

        row = QWidget()
        buttons = QHBoxLayout(row)
        buttons.setContentsMargins(0, 0, 0, 0)
        assign = _named(QPushButton("Assign asset"), "Assign asset")
        assign.clicked.connect(self.assign_asset)
        buttons.addWidget(assign)
        buttons.addStretch(1)
        form.addRow("", row)
        layout.addWidget(box)
        return page

    def assign_asset(self) -> None:
        tag = self.assign_tag.text().strip().upper()
        username = self.assign_user.text().strip()
        if not tag or not username:
            self._say("Asset tag and Assign to username are both required.", error=True)
            return

        state = STORE.load()
        asset = next((a for a in state["assets"] if a["tag"].upper() == tag), None)
        if asset is None:
            available = ", ".join(a["tag"] for a in state["assets"] if a["status"] == "available")
            self._say(
                f"No asset '{tag}' exists in the inventory. Available now: {available or 'none'}.",
                error=True,
            )
            return
        if not any(a["username"].lower() == username.lower() for a in state["accounts"]):
            self._say(
                f"No account '{username}' exists. Create the account before assigning hardware.",
                error=True,
            )
            return
        if asset["status"] == "in_repair":
            self._say(f"Asset {tag} is in repair and cannot be assigned.", error=True)
            return
        if asset["status"] == "assigned" and asset["assigned_to"].lower() != username.lower():
            self._say(
                f"Asset {tag} is already assigned to {asset['assigned_to']}. "
                "Unassign it first or choose another.",
                error=True,
            )
            return
        if asset["assigned_to"].lower() == username.lower():
            self._say(f"Asset {tag} is already assigned to {username}. No change made.")
            return

        def apply(st: dict[str, Any]) -> None:
            for item in st["assets"]:
                if item["tag"].upper() == tag:
                    item["status"] = "assigned"
                    item["assigned_to"] = username
            st.setdefault("audit", []).append(
                {"action": "asset.assigned", "tag": tag, "to": username}
            )

        STORE.mutate(apply)
        self._say(f"Asset {tag} ({asset['model']}) assigned to {username}.")
        self.assign_tag.clear()
        self.assign_user.clear()
        self.refresh()

    # -- Access Groups -----------------------------------------------------
    def _access_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.groups_list = _named(QListWidget(), "Access groups list")
        header = QLabel("Group | Restricted | Members | Description")
        _named(header, "Access groups list columns")
        header.setStyleSheet("color:#5c6b80;font-weight:600")
        layout.addWidget(header)
        layout.addWidget(self.groups_list)

        box = QGroupBox("Grant access")
        form = QFormLayout(box)
        self.grant_user = _named(QLineEdit(), "Grant to username")
        self.grant_group = _named(QComboBox(), "Access group")
        form.addRow("Grant to username", self.grant_user)
        form.addRow("Access group", self.grant_group)

        grant = _named(QPushButton("Grant access"), "Grant access")
        grant.clicked.connect(self.grant_access)
        form.addRow("", grant)
        layout.addWidget(box)

        note = QLabel(
            "Groups marked Restricted require Finance Controller approval and "
            "cannot be granted from this screen."
        )
        _named(note, "Restricted groups note")
        note.setWordWrap(True)
        note.setStyleSheet("color:#5c6b80")
        layout.addWidget(note)
        return page

    def grant_access(self) -> None:
        username = self.grant_user.text().strip()
        group = self.grant_group.currentText().strip()
        if not username or not group:
            self._say("Grant to username and Access group are both required.", error=True)
            return

        state = STORE.load()
        groups = state["access_groups"]
        if group not in groups:
            self._say(f"No access group '{group}' exists.", error=True)
            return
        if not any(a["username"].lower() == username.lower() for a in state["accounts"]):
            self._say(
                f"No account '{username}' exists. Create the account before granting access.",
                error=True,
            )
            return
        if groups[group].get("restricted"):
            # The refusal that forces a human into the loop.
            self._say(
                f"Group '{group}' is restricted and requires Finance Controller approval. "
                "It cannot be granted from this screen. Raise an approval request first.",
                error=True,
            )
            return
        if username.lower() in {m.lower() for m in groups[group]["members"]}:
            self._say(f"{username} is already a member of '{group}'. No change made.")
            return

        def apply(st: dict[str, Any]) -> None:
            st["access_groups"][group]["members"].append(username)
            st.setdefault("audit", []).append(
                {"action": "access.granted", "group": group, "username": username}
            )

        STORE.mutate(apply)
        self._say(f"{username} added to access group '{group}'.")
        self.grant_user.clear()
        self.refresh()

    # -- rendering ---------------------------------------------------------
    def refresh(self) -> None:
        state = STORE.load()

        self._fill(
            self.accounts_list,
            [
                [a["username"], a["full_name"], a["role"], a["department"], a["status"]]
                for a in state.get("accounts", [])
            ],
        )
        self._fill(
            self.assets_list,
            [
                [a["tag"], a["type"], a["model"], a["status"], a["assigned_to"] or "-"]
                for a in state.get("assets", [])
            ],
        )
        groups = state.get("access_groups", {})
        self._fill(
            self.groups_list,
            [
                [
                    name,
                    "restricted" if info.get("restricted") else "open",
                    ", ".join(info.get("members", [])) or "no members",
                    info.get("description", ""),
                ]
                for name, info in groups.items()
            ],
        )

        current = self.grant_group.currentText()
        if [self.grant_group.itemText(i) for i in range(self.grant_group.count())] != list(groups):
            self.grant_group.clear()
            self.grant_group.addItems(list(groups))
            if current in groups:
                self.grant_group.setCurrentText(current)

    @staticmethod
    def _fill(view: QListWidget, rows: list[list[str]]) -> None:
        """Render rows as list items.

        A list rather than a table because Qt does not expose QTableWidget's data
        cells to UIA -- only the column headers come through, so the operator
        could see the shape of the data but never its contents. List items expose
        their text as the item's accessible name, which is readable.
        """
        rendered = [" | ".join(str(value) for value in row) for row in rows]
        if [view.item(i).text() for i in range(view.count())] == rendered:
            return  # avoid churn: the poll timer calls this every 1.5s
        view.clear()
        view.addItems(rendered)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(WINDOW_TITLE)
    window = AssetManager()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

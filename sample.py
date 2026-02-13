import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

import requests

API_BASE = "https://api.kraken.com/0/public"
# Known fiat and stable asset altnames on Kraken. Extend as needed.
STABLE_FIAT_ALTNAMES = {
    "USD",
    "EUR",
    "GBP",
    "CAD",
    "CHF",
    "JPY",
    "AUD",
    "USDT",
    "USDC",
    "DAI",
    "USDP",
}


def fetch_assets():
    resp = requests.get(f"{API_BASE}/Assets", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data["result"]


def fetch_asset_pairs():
    resp = requests.get(f"{API_BASE}/AssetPairs", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data["result"]


class KrakenGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Kraken Pairs Browser")
        self.root.geometry("520x520")
        self.root.resizable(False, False)

        self.assets = {}
        self.assets_by_alt = {}
        self.asset_pairs = None

        self._build_ui()
        self._load_assets()

    def _build_ui(self):
        padding = {"padx": 10, "pady": 6}

        ttk.Label(
            self.root, text="Select a fiat/stable asset to see available crypto pairs:", wraplength=480
        ).grid(row=0, column=0, sticky="w", **padding)

        self.stable_var = tk.StringVar()
        self.combo = ttk.Combobox(self.root, textvariable=self.stable_var, state="readonly", width=25)
        self.combo.grid(row=1, column=0, sticky="we", **padding)
        self.combo.bind("<<ComboboxSelected>>", self._on_stable_selected)

        ttk.Button(self.root, text="Refresh", command=self._refresh).grid(row=1, column=1, sticky="e", **padding)

        ttk.Label(self.root, text="Available crypto bases paired with selection:").grid(
            row=2, column=0, columnspan=2, sticky="w", **padding
        )

        self.listbox = tk.Listbox(self.root, height=20, width=40)
        self.listbox.grid(row=3, column=0, columnspan=2, sticky="nsew", **padding)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status_var).grid(row=4, column=0, columnspan=2, sticky="w", **padding)

        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(3, weight=1)

    def _load_assets(self):
        try:
            self.assets = fetch_assets()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Error", f"Failed to load assets: {exc}")
            self.status_var.set("Failed to load assets")
            return

        self.assets_by_alt = {info.get("altname"): name for name, info in self.assets.items() if info.get("altname")}

        available = sorted([alt for alt in self.assets_by_alt if alt in STABLE_FIAT_ALTNAMES])
        if not available:
            messagebox.showwarning("Warning", "No stable/fiat assets found in Kraken assets list.")
            self.status_var.set("No fiat/stable assets available")
            return

        self.combo["values"] = available
        self.combo.current(0)
        self.status_var.set("Assets loaded. Select a quote asset to view pairs.")
        self._on_stable_selected()

    def _on_stable_selected(self, event=None):  # noqa: ARG002
        quote_alt = self.stable_var.get()
        if not quote_alt:
            return

        quote_code = self.assets_by_alt.get(quote_alt)
        if not quote_code:
            messagebox.showerror("Error", f"Could not resolve asset code for {quote_alt}")
            return

        if self.asset_pairs is None:
            try:
                self.asset_pairs = fetch_asset_pairs()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Error", f"Failed to load asset pairs: {exc}")
                self.status_var.set("Failed to load pairs")
                return

        bases = []
        for pair_info in self.asset_pairs.values():
            if pair_info.get("quote") != quote_code:
                continue
            if pair_info.get("status") not in (None, "online"):
                continue
            base_code = pair_info.get("base")
            base_alt = self.assets.get(base_code, {}).get("altname") or pair_info.get("base")
            bases.append(base_alt)

        unique_bases = sorted(set(bases))
        self.listbox.delete(0, tk.END)
        for asset in unique_bases:
            self.listbox.insert(tk.END, asset)

        self.status_var.set(f"Found {len(unique_bases)} pairs for {quote_alt}")

    def _refresh(self):
        self.asset_pairs = None
        self.listbox.delete(0, tk.END)
        self.status_var.set("Refreshing data...")
        self._on_stable_selected()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    gui = KrakenGUI()
    gui.run()

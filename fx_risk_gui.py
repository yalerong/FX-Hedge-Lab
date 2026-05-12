from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from fx_risk_simulator import build_report, explain_report, load_case


class FxRiskApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("外汇风险与套保模拟")
        self.geometry("1040x720")
        self.minsize(900, 620)

        self.case_path = tk.StringVar(value=str(Path(__file__).with_name("sample_data.json")))

        self._build_toolbar()
        self._build_tabs()
        self.load_and_run()

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self, padding=(10, 8))
        toolbar.pack(fill=tk.X)

        ttk.Label(toolbar, text="数据文件").pack(side=tk.LEFT)
        path_entry = ttk.Entry(toolbar, textvariable=self.case_path)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        ttk.Button(toolbar, text="选择 JSON", command=self.choose_file).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(toolbar, text="运行模拟", command=self.load_and_run).pack(side=tk.LEFT)

    def _build_tabs(self) -> None:
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.summary = self._text_tab("摘要")
        self.beginner = self._text_tab("白话解释")
        self.strategy = self._tree_tab(
            "套保策略",
            ("hedge_ratio", "worst_case_cny", "pnl_stddev_cny", "scenario_pnl_cny"),
            ("套保比例", "最差情景损益 CNY", "波动 CNY", "各情景损益"),
        )
        self.exposure = self._tree_tab(
            "敞口",
            ("scope", "period_currency", "amount"),
            ("口径", "期间/币种", "金额"),
        )
        self.validation = self._tree_tab(
            "自动校验",
            ("level", "message", "details"),
            ("级别", "问题", "明细"),
        )
        self.raw = self._text_tab("原始输出")

    def _text_tab(self, title: str) -> tk.Text:
        frame = ttk.Frame(self.tabs)
        text = tk.Text(frame, wrap=tk.WORD, font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tabs.add(frame, text=title)
        return text

    def _tree_tab(self, title: str, columns: tuple[str, ...], headings: tuple[str, ...]) -> ttk.Treeview:
        frame = ttk.Frame(self.tabs)
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, heading in zip(columns, headings):
            tree.heading(column, text=heading)
            tree.column(column, width=180, anchor=tk.W)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tabs.add(frame, text=title)
        return tree

    def choose_file(self) -> None:
        filename = filedialog.askopenfilename(
            title="选择外汇模拟 JSON 数据",
            initialdir=str(Path(self.case_path.get()).parent),
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if filename:
            self.case_path.set(filename)
            self.load_and_run()

    def load_and_run(self) -> None:
        try:
            report = build_report(load_case(Path(self.case_path.get())))
        except Exception as exc:
            messagebox.showerror("运行失败", str(exc))
            return

        self._render_summary(report)
        self._set_text(self.beginner, explain_report(report))
        self._render_strategy(report)
        self._render_exposure(report)
        self._render_validation(report)
        self._set_text(self.raw, json.dumps(report, ensure_ascii=False, indent=2))

    def _render_summary(self, report: dict) -> None:
        recommended = report.get("recommended_strategy") or {}
        lines = [
            "模拟链路",
            "1. 采集业务、财务、交易和汇率数据",
            "2. 识别交易风险、折算风险和经济风险",
            "3. 按期间和币种汇总净敞口",
            "4. 在不同汇率情景下模拟损益",
            "5. 比较不同套保比例",
            "6. 自动校验交易汇率和财务口径损益",
            "",
            f"推荐套保比例：{recommended.get('hedge_ratio', '-')}",
            f"最差情景损益 CNY：{recommended.get('worst_case_cny', '-')}",
            f"情景损益波动 CNY：{recommended.get('pnl_stddev_cny', '-')}",
            f"校验问题数量：{len(report.get('validation_issues', []))}",
        ]
        self._set_text(self.summary, "\n".join(lines))

    def _render_strategy(self, report: dict) -> None:
        self._clear_tree(self.strategy)
        for row in report.get("strategy_rank", []):
            self.strategy.insert(
                "",
                tk.END,
                values=(
                    row.get("hedge_ratio"),
                    row.get("worst_case_cny"),
                    row.get("pnl_stddev_cny"),
                    json.dumps(row.get("scenario_pnl_cny", {}), ensure_ascii=False),
                ),
            )

    def _render_exposure(self, report: dict) -> None:
        self._clear_tree(self.exposure)
        for scope, label in (
            ("unhedged_exposure", "未套保"),
            ("exposure_after_executed_trades", "已执行交易后"),
        ):
            for period_currency, amount in report.get(scope, {}).items():
                self.exposure.insert("", tk.END, values=(label, period_currency, amount))

    def _render_validation(self, report: dict) -> None:
        self._clear_tree(self.validation)
        issues = report.get("validation_issues", [])
        if not issues:
            self.validation.insert("", tk.END, values=("ok", "未发现校验问题", ""))
            return
        for issue in issues:
            details = {key: value for key, value in issue.items() if key not in {"level", "message"}}
            self.validation.insert(
                "",
                tk.END,
                values=(
                    issue.get("level", ""),
                    issue.get("message", ""),
                    json.dumps(details, ensure_ascii=False),
                ),
            )

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, value)
        widget.configure(state=tk.DISABLED)

    @staticmethod
    def _clear_tree(tree: ttk.Treeview) -> None:
        for item in tree.get_children():
            tree.delete(item)


if __name__ == "__main__":
    FxRiskApp().mainloop()

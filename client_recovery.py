"""Interactive administrator recovery entry point (package as a windowed EXE)."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path

from ksat.client.recovery import ClientRecovery, PROBLEM_GUIDANCE, RecoveryError
from ksat.client.version import CLIENT_VERSION


def run_gui(backend: ClientRecovery) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    window = tk.Tk()
    window.title('KSAT Client Recovery — Administrator')
    window.geometry('850x540')
    ttk.Label(window, text='Archive obsolete practice attempts', font=('', 16, 'bold')).pack(pady=12)
    ttk.Label(window, wraplength=810, text=(
        'Select only old practice attempts confirmed by faculty. A protected local archive is verified first. '
        'Selected attempts will be removed from the live queue and will not upload or appear as graded results. '
        'Active tests cannot be interrupted.')).pack(padx=18)
    details = tk.StringVar()
    ttk.Label(window, textvariable=details, wraplength=810).pack(padx=18, pady=12)
    tree = ttk.Treeview(window, columns=('attempt', 'sealed'), show='headings', selectmode='extended')
    tree.heading('attempt', text='Attempt ID')
    tree.heading('sealed', text='Sealed at (UTC)')
    tree.column('attempt', width=420)
    tree.pack(fill='both', expand=True, padx=18)
    confirmed = tk.BooleanVar(value=False)
    ttk.Checkbutton(window, variable=confirmed,
                    text='I confirm the selected attempts are obsolete practice work.').pack(pady=10)
    candidates = {}
    buttons = ttk.Frame(window)
    buttons.pack(pady=10)

    def refresh():
        candidates.clear()
        tree.delete(*tree.get_children())
        confirmed.set(False)
        preview = backend.preview()
        for candidate in preview.candidates:
            candidates[candidate.attempt_id] = candidate
            tree.insert('', 'end', iid=candidate.attempt_id, values=(candidate.attempt_id, candidate.sealed_at))
        details.set('\n'.join(f'{code}: {PROBLEM_GUIDANCE[code]}' for code in preview.problem_codes)
                    or 'No eligible blocked practice attempts found. No repair is needed here.')
        archive_button.configure(state='disabled' if 'active_attempt' in preview.problem_codes or not candidates else 'normal')

    def recover():
        selected = [candidates[i] for i in tree.selection()]
        if not selected or not confirmed.get():
            messagebox.showwarning('Confirmation required', PROBLEM_GUIDANCE['confirmation_required'], parent=window)
            return
        ids = '\n'.join(c.attempt_id for c in selected)
        if not messagebox.askyesno('Archive and remove practice work?',
                f'Archive and remove these {len(selected)} practice attempts from the live queue?\n\n{ids}\n\n'
                'They will not upload or become graded results.', parent=window):
            return
        archive_button.configure(state='disabled')
        window.update_idletasks()
        try:
            result = backend.archive_practice(selected, confirmed_practice=True)
        except RecoveryError as error:
            text = f'{error.code}: {PROBLEM_GUIDANCE[error.code]}'
            if error.archive_path:
                text += f'\n\nRetained archive: {error.archive_path}'
            messagebox.showerror('Recovery needs attention', text, parent=window)
        else:
            messagebox.showinfo('Practice attempts archived',
                f'Removed {len(result.removed_attempt_ids)} practice attempts.\n\nProtected archive: {result.archive_path}', parent=window)
        refresh()

    ttk.Button(buttons, text='Refresh diagnosis', command=refresh).pack(side='left', padx=6)
    archive_button = ttk.Button(buttons, text='Archive selected practice attempts', command=recover)
    archive_button.pack(side='left', padx=6)
    ttk.Button(buttons, text='Close', command=window.destroy).pack(side='left', padx=6)
    refresh()
    window.mainloop()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='KSATClientRecovery', description='Administrator practice-attempt recovery. Normal startup opens the recovery window.')
    parser.add_argument('--version', action='version', version=f'KSAT Client Recovery {CLIENT_VERSION}')
    parser.parse_args(argv)
    import tkinter as tk
    from tkinter import messagebox
    if os.name != 'nt' or not ctypes.windll.shell32.IsUserAnAdmin():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('Administrator required', 'Run KSAT Client Recovery as administrator from the Start menu.', parent=root)
        root.destroy()
        return 1
    return run_gui(ClientRecovery(Path(os.environ['ProgramData']) / 'KSAT Client'))


if __name__ == '__main__':
    raise SystemExit(main())

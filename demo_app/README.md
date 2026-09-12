# AI-interface Banking Website Guide

This is the user guide for the synthetic banking back-office website included with the Computer-Use Automation System.

## Open the website

Production website: [https://interfaceai.vercel.app](https://interfaceai.vercel.app)

The application contains invented training records only. Never enter real customer or personal information.

## Start a session

1. Open the production website.
2. Review the training-environment notice.
3. Select **Begin Secure Session**.
4. The application opens the **Member Search** screen.

No username or password is required because this is an isolated demonstration application, not a real banking system.

## Search for a member

1. Open **Member Search** from the operations menu.
2. Enter a five-digit synthetic member ID.
3. Select **Find Member**.

Use these test records:

| Member ID | Expected behavior |
| --- | --- |
| `12345` | Normal member with checking and savings accounts |
| `70007` | Successful search with intentionally slow loading |
| `80008` | Permission-denied screen |
| `90009` | Session-expired screen |
| Any other five digits | Member-not-found business outcome |
| Invalid format | Validation error |

The normal member screen displays the current and available balances for both accounts.

## Transfer demonstration

1. Search for member `12345`.
2. Select **Transfer Funds**.
3. Choose different source and destination accounts.
4. Enter a positive amount within the available balance.
5. Select **Review Transfer**.
6. Verify the confirmation screen before selecting **Submit Transfer**.

The confirmation page is intentional: funds have not moved at the review stage. All submissions are synthetic and do not perform a real financial transaction.

Useful validation cases:

- Choose the same source and destination to see a validation error.
- Enter more than the available balance to see an insufficient-funds validation error.
- Enter `13.37` to trigger the unexpected confirmation-dialog fixture.

## Operations menu

- **Member Search** locates an invented member record by ID.
- **Account Maintenance** displays the synthetic account-review queue. Maintenance writes are intentionally disabled.
- **Daily Reports** displays fixed operational reports and control totals.
- **System Messages** displays training notices and procedural reminders.
- **End Session** clears the training session and returns to the expired-session screen.

## Run locally

From the repository root after completing the installation in the main README:

```bash
uvicorn demo_app.app:app --host 127.0.0.1 --port 8001
```

Then open [http://127.0.0.1:8001](http://127.0.0.1:8001).

## Scope and limitations

The hosted website is the automation target only. LLM discovery, Playwright execution, deterministic replay, evidence capture, and same-session human takeover run from the local Python project because they require a retained browser process and local evidence storage.

The application deliberately resembles an older enterprise interface: it uses tables, inconsistent markup, and controls identified through accessible names, labels, or visible text rather than test IDs. This makes it a realistic target for demonstrating robust locator strategies.

For developer setup and automation commands, see the [project README](../README.md).

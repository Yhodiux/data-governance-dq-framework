# Source Baseline: PKDD'99 Czech Financial Dataset

## Dataset identification

- **Name:** PKDD'99 Czech Financial Dataset (Berka Dataset)
- **Project identifier:** `pkdd99-czech-financial-dataset`
- **Reference:** PKDD'99 Discovery Challenge financial dataset, prepared by Petr Berka and Marta Sochorova.

## Purpose in this project

The dataset provides a compact relational banking domain for the incremental development of an executable, metadata-driven Data Governance and Data Quality framework. At this stage it is used only to establish an immutable source baseline. No profiling conclusions or Data Quality results are asserted here.

## Source files and verified integrity

Counts exclude the header row.

| File | Documented entity | Records | SHA-256 |
|---|---|---:|---|
| `account.asc` | Account | 4,500 | `58D7F50ABD72E9B1A5568346F74BB54CD71224EE1DB9F09A27D7CAC563F38CC6` |
| `card.asc` | Credit card | 892 | `FC669BDE6ADF6457D87421C0BFB218E9C384A7032C6ACCD348D207A405E72109` |
| `client.asc` | Client | 5,369 | `E435C6B92D246F4F0DFD5E2827469D745C06238714C32B3FFB415EEBE794E1A7` |
| `disp.asc` | Disposition | 5,369 | `EBD801F77B6D322E8EBC08E52F188E7C8FCA539325F85F57F8C73434DA9D32D8` |
| `district.asc` | District demographic data | 77 | `7F03CF3B9B82F0FDCC3ABDF6CC716F145DB8E9875C68E2D2E2F7151E9ECF4DF3` |
| `loan.asc` | Loan | 682 | `68535F609A254AA7A3F03DD8E27DCB822B532DF12A0D6046F0666B8DC0B8AE8E` |
| `order.asc` | Permanent order | 6,471 | `035930FA6ACD2CA42A935E654B21E1BB260248F49B6DC6E7DE6351B7C4D56D02` |
| `trans.asc` | Transaction | 1,056,320 | `75AB2F39DF9D79D79C5C900DE90DDD28248B689F214598AC9FA2FF0F574A70D2` |

The listed counts and checksums were verified against the local source files without modifying them.

## Physical format

- The source consists of semicolon-delimited `.asc` text files with headers.
- Text values may be enclosed in double quotes.
- The observed decimal separator is a period (`.`).
- Most documented date fields use `YYMMDD`.
- `card.issued` physically appears as `YYMMDD 00:00:00`, although the source guide documents the date portion as `YYMMDD`.
- Empty or missing values may have different physical representations. No representation has been normalized or interpreted at this stage.
- `district.asc` uses the physical column names `A1` through `A16`.

## Documented entities and field semantics

The supplied source guide documents these relations and fields:

- **Account:** `account_id` identifies the account; `district_id` is the branch location; `frequency` is statement issuance frequency; `date` is the account creation date.
- **Client:** `client_id` identifies the record; `birth_number` encodes birth date and sex according to the source convention; `district_id` is the client's address district.
- **Disposition:** `disp_id` identifies the record; `client_id` identifies a client; `account_id` identifies an account; `type` distinguishes owner and user. The guide states that only an owner may issue permanent orders and request a loan.
- **Permanent order:** `order_id` identifies the record; `account_id` is the originating account; `bank_to` and `account_to` identify the recipient bank and account; `amount` is the debited amount; `k_symbol` characterizes the payment.
- **Transaction:** `trans_id` identifies the record; `account_id` identifies the affected account; `date` is the transaction date; `type` indicates credit or withdrawal; `operation` describes the transaction mode; `amount` is the transaction amount; `balance` is the balance after the transaction; `k_symbol` characterizes the transaction; `bank` and `account` identify the partner bank and account.
- **Loan:** `loan_id` identifies the record; `account_id` identifies the account; `date` is the grant date; `amount` is the loan amount; `duration` is the loan duration; `payments` is the monthly payment; `status` describes repayment status.
- **Credit card:** `card_id` identifies the record; `disp_id` identifies the disposition to an account; `type` is the card type; `issued` is the issue date.
- **District demographic data:** `A1` is the district code; `A2` the district name; `A3` the region; `A4` the number of inhabitants; `A5`-`A8` municipality counts by population band; `A9` the number of cities; `A10` the ratio of urban inhabitants; `A11` average salary; `A12` and `A13` unemployment rates for 1995 and 1996; `A14` entrepreneurs per 1,000 inhabitants; and `A15` and `A16` committed crimes in 1995 and 1996.

The source guide also defines coded values for statement frequency, disposition type, payment and transaction characterization, transaction type and operation, loan status, and card type. This baseline records that those source-defined semantics exist; it does not transform or standardize their physical values.

## Documented relationships

- `disp.client_id` relates a disposition to `client.client_id`.
- `disp.account_id` relates a disposition to `account.account_id`.
- `order.account_id`, `trans.account_id`, and `loan.account_id` relate those records to an account.
- `card.disp_id` relates a credit card to a disposition.
- `account.district_id` represents the branch district, and `client.district_id` represents the client's address district; `district.A1` is the documented district code.

The source guide states that a client may have multiple accounts and an account may be manipulated by multiple clients through dispositions. Multiple cards may be issued to an account, while at most one loan may be granted for an account. These statements are documented source semantics, not measured referential-integrity results.

## Known limitations of this baseline

- It contains no computed column statistics, distributions, null rates, uniqueness measures, duplicate counts, or referential-integrity measurements.
- It makes no assertion that missing values, duplicates, broken relationships, or other Data Quality problems exist.
- It does not resolve or normalize source-language coded values.
- It does not assign business owners, stewards, domains, Critical Data Elements, classifications, policies, or Data Quality rules.
- Two-digit years require an interpretation policy in a future standardization phase; none is selected here.

## Evidence classification

| Category | Meaning in this project | Status at this stage |
|---|---|---|
| Documented source facts | Statements present in the supplied dataset guide, including entity, field, code, and relationship descriptions. | Recorded above and kept distinct from measurement. |
| Observed physical characteristics | Facts directly inspected in the supplied files, such as delimiters, headers, representations, counts, and hashes. | Recorded above as source-baseline observations. |
| Future profiling findings | Measurements produced by a future profiling process. | None produced or claimed. |
| Project assumptions | Interpretations introduced when evidence is absent. | None adopted in this baseline. |
| Governance metadata | Project decisions such as domains, ownership, stewardship, CDEs, and classifications. | None introduced. |

The data zones remain distinct: **SOURCE** is the unchanged original input; **RAW** will be produced only by a future ingestion process; **PROFILED METADATA** will contain measured findings; **GOVERNANCE METADATA** will contain explicit project decisions; and **TRUSTED** will contain future standardized or validated data.


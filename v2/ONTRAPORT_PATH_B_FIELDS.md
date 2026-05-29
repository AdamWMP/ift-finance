# ONtraport — Path B field schema for follow-on streams

So the dashboard can drill into NutriCert / PPN / FBA / S&C the same way it
drills into PT / Pilates / Reformer, each follow-on stream needs the same
shape of custom-field set. Most streams already have *some* fields. This doc
lists only the **missing** fields.

> ⚠️ ONtraport does not expose custom-field creation through its public API.
> Add these in the admin UI:
> **Contacts → Manage Custom Fields → (Section) → Add Field**.
> After adding, hit ↻ Refresh on the finance dashboard and run a manual sync —
> the discovery query will start picking up the new contacts automatically.

## Current state (already in ONtraport)

| Stream | Year | Term | Start | Location | Timetable | Qualification | Plan | Price | Spent | Method |
|---|---|---|---|---|---|---|---|---|---|---|
| **PT**       | ✅ f2288 | ✅ f2289 | ✅ f2293 | ✅ f2291 | ✅ f2292 | ✅ f2290 | ✅ f2296 | ✅ f2294 | ✅ f2334 | ✅ f2537 |
| **Pilates**  | ✅ f2300 | ✅ f2301 | ✅ f2305 | ✅ f2303 | ✅ f2304 | ✅ f2302 | ✅ f2309 | ✅ f2306 | ✅ f2335 | ✅ f2538 |
| **Reformer** | ✅ f2590 | ✅ f2591 | ✅ f2595 | ✅ f2593 | ✅ f2594 | ✅ f2592 | ✅ f2598 | ✅ f2596 | ✅ f2599 | ❌ add |
| **S&C**      | ❌ add  | ❌ add  | ✅ f2315 | ✅ f2316 | ❌ add  | ✅ f2317 | ✅ f2321 | ✅ f2319 | ✅ f2322 | ❌ add |
| **PPN**      | ❌ add  | ❌ add  | ❌ add  | ❌ add  | ❌ add  | ✅ f2323 | ✅ f2326 | ✅ f2324 | ✅ f2327 | ❌ add |
| **AN / NutriCert** | ❌ add | ❌ add | ❌ add | ❌ add | ❌ add | ✅ f2329 | ✅ f2332 | ✅ f2330 | ✅ f2333 | ❌ add |
| **FBA**      | ❌ add  | ❌ add  | ✅ f2615 | ❌ add  | ❌ add  | ❌ add  | ❌ add  | ✅ f2616 | ✅ f2617 | ❌ add |

## Question to confirm

- **Is "NutriCert" the same as the Advanced Nutrition (AN) stream**, or is
  NutriCert a separate course that needs its own field group? The Sales Board
  category `nutricert` doesn't match anything in ONtraport today.

## Fields to add

### Reformer · 1 new field

| Field name (alias)              | Type | Options |
|---|---|---|
| Reformer Payment Method         | Drop | Cash, Stripe, Revolut, Bank Transfer, DSP, Partial Cash & Stripe, Cash & DSP Transfer |

### S&C · 4 new fields

| Field name (alias)         | Type | Options |
|---|---|---|
| S&C Course Year            | Drop | 2024, 2025, 2026 |
| S&C Course Term            | Drop | Autumn (July–Nov), Summer (May–June), Spring (Dec–Apr) |
| S&C Course Timetable       | Drop | Thursday & Friday 10–4:30pm, Saturday & Sunday 10–4:30pm, Phase 1 & 2 (5 Days) |
| S&C Payment Method         | Drop | (same options as PT/Pilates payment method) |

### PPN · 7 new fields

| Field name (alias)         | Type | Options |
|---|---|---|
| PPN Course Year            | Drop | 2024, 2025, 2026 |
| PPN Course Term            | Drop | Autumn, Summer, Spring |
| PPN Start Date             | Full date | — |
| PPN Course Location        | Drop | Online (default), Cork, Galway, Dublin – Swords, Dublin – Tallaght |
| PPN Course Timetable       | Drop | Monday Evenings Online 7–9pm, Tuesday & Thursday Evenings (Online), Saturday & Sunday In-Person |
| PPN Payment Method         | Drop | (same options as PT/Pilates payment method) |
| PPN Qualification (label)  | Drop | Pre and Post Natal Online Course (default) |

> Note: `f2323 PPN Course` is already a drop with one option ("Pre and Post
> Natal Online Course"); we can keep that as the "qualification" field — no
> new field needed if so.

### AN / Advanced Nutrition (a.k.a. NutriCert?) · 6 new fields

| Field name (alias)         | Type | Options |
|---|---|---|
| AN Course Year             | Drop | 2024, 2025, 2026 |
| AN Course Term             | Drop | Autumn, Summer, Spring |
| AN Start Date              | Full date | — |
| AN Course Location         | Drop | Online, Cork, Galway, Dublin – Swords, Dublin – Tallaght |
| AN Course Timetable        | Drop | Monday & Wednesday Online 7–9pm, In-person Weekend Intensives |
| AN Payment Method          | Drop | (same options as PT/Pilates payment method) |

(Existing `f2329 AN Course` drop is the qualification label.)

### FBA · 7 new fields

| Field name (alias)         | Type | Options |
|---|---|---|
| FBA Course Year            | Drop | 2024, 2025, 2026 |
| FBA Course Term            | Drop | Autumn, Summer, Spring |
| FBA Course Location        | Drop | Online, Cork, Galway, Dublin – Swords, Dublin – Tallaght |
| FBA Course Timetable       | Drop | — fill in real options |
| FBA Qualification          | Drop | Fitness Business Accelerator (default) |
| FBA Payment Plan           | Text | — |
| FBA Payment Method         | Drop | (same options as PT/Pilates payment method) |

## After fields exist — code changes I'll ship

1. **Add the new field IDs** to `app/ontraport.py` (constants block at top).
2. **Extend `discover_s26_contact_ids()`** so the search query becomes
   `f2288=586 OR f2300=587 OR f2590=616 OR <S&C year>=2026 OR <PPN year>=2026 OR <AN year>=2026 OR <FBA year>=2026`.
3. **Extend the per-contact decode** in `op_row()` to emit one `students` row
   per stream the contact is enrolled in (S&C / PPN / AN / FBA in addition to
   PT / Pilates / Reformer).
4. **Add stream slugs** to the dashboard taxonomy so S&C/PPN/AN/FBA show up as
   first-class streams. The new fields also let me pull a real Payment Method
   per stream, which fixes the missing "Sales Board (other streams)" row in
   the Money-In panel — those rows will finally be properly bucketed by
   method (Stripe / Cash / etc).
5. **Cert export** — S&C will become eligible (currently blocked because we
   have no per-student S&C rows in the DB).

## Estimated effort

| Step | Time |
|---|---|
| Add 25 fields in ONtraport UI (you, with my list) | 10–15 min |
| Add fields one-time via teach mode (me walking you through) | 25 min |
| Code wiring after fields exist (me) | 30 min |
| Re-discovery + first ingest of new contacts (sync) | 5 min |

## What it unlocks

- Drill-down into NutriCert / PPN / FBA / S&C from "By stream" panel
- S&C cert export (the user explicitly requested this)
- Reconciled "How money has come in" (no more synthetic Sales Board row)
- Sales Board can stop being the source of truth for these revenue streams —
  ONtraport becomes authoritative for the *whole* business.

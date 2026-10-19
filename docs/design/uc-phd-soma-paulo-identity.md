# uc-phd — "Soma, Paulo" is one person, not three

Product Owner resolution for item #3 of Kanban card
`3fe5bf3b-9510-8127-b32c-c47722c20c65`, settled by external web research on
2026-10-19. Repo this concerns: `/opt/aw-workspace/repos/aw-app-uc-phd`.

This is a **findings document, not a design**. It closes a question the
predecessor card (`3fe5bf3b-9510-8137-b4cc-f53bbbbe4ec4`) left open with
"evidence, not proof", and it exists so nobody re-investigates it. **No code
change follows from it** — see §4 for why, which is itself the finding.

## 0. The question

Three theses in the corpus carry the author string `Soma, Paulo`:

| handle | title | date | faculty / branch | supervisor(s) |
|---|---|---|---|---|
| `10316/87615` | Políticas públicas de urbanismo em Angola | 2018-10-22 | **FEUC** — Sociologia, Cidades e Culturas Urbanas | Peixoto, Paulo |
| `10316/114285` | Raízes, Representações e Afirmação da Soberania Política no Contexto Geral e de Angola | 2023-05-04 | **FLUC** — Filosofia, Filosofia Política | Sá, Alexandre Franco de; Ferrer, Diogo |
| `10316/117180` | Paradigmas de codificação e sistematização do direito… pluralismo jurídico | 2024-10-17 | **FDUC** — Direito, Ciências Jurídico-Filosóficas | Marques, Mário Reis |

All three `dc:type=info:eu-repo/semantics/doctoralThesis` (raw OAI, verified).
Three faculties, three non-overlapping supervisor sets, six years. The prior
investigation read that as "implausible as one person's trajectory — likely
three different people", and said so explicitly as a guess.

**That guess was wrong.**

## 1. Verdict: ONE person — Paulo Soma, an Angolan jurist with three UC doctorates

Sociology (FEUC, June 2018) → Philosophy (FLUC, December 2022, deposited
2023) → Law (FDUC, February 2024). Not a matcher artefact, not a harvest
artefact, not a name coincidence. The corpus is correct as it stands.

## 2. The evidence

Documentary, from the theses' own full texts, pulled directly from Estudo
Geral (the bitstream cookie gate in
[[estudogeral-bitstreams-need-a-browser-check-cookie]] applies — item page
first, then the bitstream, in one session).

**(a) The 2024 Law thesis claims the 2018 Sociology thesis as its own author's
work, in the first person.** `direito2024` footnote 1066:

> Sobre a problemática, vide **o nosso**, *Políticas Públicas de Urbanismo em
> Angola*, Lousã, Angolanae Dissertationes, 2019.

`o nosso` ("our own") is the standard Portuguese academic self-citation
formula. The work named is the published (2019) edition of the 2018 FEUC
thesis, whose title is verbatim *Políticas públicas de urbanismo em Angola*.
Its bibliography repeats it as `SOMA, Paulo, Políticas Públicas de Urbanismo
em Angola, Lousã, Angolanae Dissertationes, 2019`.

The same thesis claims two further works the same way — footnote 920, `vide,
neste sentido, **o nosso**, Para Compreender o Notariado em Angola, Porto,
Ecopy, 2013`, and footnote 34, `vide também **o nosso**: O Estado-Nação, a
Soberania e a Codificação na Modernidade, Porto, Edições Ecopy, 2014`. So the
2024 author is a notarial-law author since 2013 who had already published on
**sovereignty** in 2014 — which is the 2023 Philosophy thesis's entire subject.

**(b) The 2018 Sociology thesis's author is a jurist, publishing through the
same publisher.** Its bibliography: `SOMA, Paulo (2016), A Relação entre os
atos de comércio e os mercados informais em Angola. Coimbra, Angolanae
Dissertaciones (Dissertação de Mestrado em Ciências Jurídico-Empresariais, sob
orientação do Prof. Dr. Filipe Casiano dos Santos)`, self-referenced in the
first person in a footnote — "*altura em que **desenvolvemos a nossa tese de
mestrado***… Cfr. SOMA, 2012". A law-and-business master's at UC, published by
Angolanae Dissertationes, the same house that published the urbanism volume
the Law thesis claims. The acknowledgements place him as an Angolan state
employee released from duty to study, funded by Angola's Instituto Nacional de
Bolsas de Estudo, thanking the then Minister of Territorial Administration by
name.

**(c) The 2023 Philosophy thesis cites the urbanism work by name**
(`Cfr. Paulo SOMA, as políticas públicas sobre o Urbanismo em Angola, 2016`,
and in its bibliography), is specialised in **Filosofia Política**, frames its
own subject as belonging to "a história da ciência jurídica (direito…)", and
is about Angolan political sovereignty — the 2014 book in (a). Same name, same
single-country focus, same sub-field as the 2024 Law thesis, adjacent dates,
and it cites the 2018 work.

(b)+(c) are corroborating; **(a) is the load-bearing one** and it is explicit
and first-person. A person claiming another person's doctoral thesis as "o
nosso" inside a UC doctoral thesis, in a field where the supervisor would
notice, is not a scenario worth entertaining.

Public corroboration of the same conclusion: CES's own *Cities and Urban
Cultures* programme page lists the 2018 thesis under the author "Paulo Soma"
(<https://ces.uc.pt/en/doutoramentos/programas-de-doutoramento/cidades-e-culturas-urbanas/teses>).
Thesis PDFs: [2018](https://estudogeral.uc.pt/handle/10316/87615),
[2023](https://estudogeral.uc.pt/handle/10316/114285),
[2024](https://estudogeral.uc.pt/handle/10316/117180).

## 3. What the external registries do NOT give us — report this, don't paper over it

The card asked specifically for Ciência Vitae / ORCID / UC faculty pages.
Searched; they do not settle it, and that is a fact about the sources, not a
gap in the answer:

* **Ciência Vitae — no profile.** Expected: it is Portugal's national
  researcher-CV registry and Paulo Soma is an Angolan graduate affiliated in
  Angola, not a Portuguese-institution researcher.
* **ORCID — none found**, and none asserted in any of the three records.
* **UC faculty directories — absent, correctly.** He is a *graduate* of three
  UC faculties, never staff at any of them, so the FDUC/FLUC/FEUC directory
  harvest the app already runs would never hold him. His academic affiliation
  is Angolan (the Law thesis's milieu is UAN / Universidade Católica de
  Angola).
* **UC's own repository holds no identity assertion either way.** The
  `oai_cerif_openaire` records for all three handles carry
  `<oai_cerif:Author><DisplayName>Soma, Paulo</DisplayName><Person /></Author>`
  — an **empty** `Person` element on every one. Estudo Geral's CRIS has no
  authority record for him, so it neither merges nor separates the three. Any
  claim that "the repository treats them as one person" would be false.

So the answer could not have come from a registry lookup. It came from reading
the theses.

## 4. No code change follows — and that is the second finding

An entry in `data/multi_thesis_authors.json` is the obvious instinct (it is how
the João Campos case is recorded) and it is **structurally impossible here**:

* all three `thesis_people` rows are `match_status='unmatched'`,
  `person_slug=NULL`. `Soma` has zero rows in the CISUC `people` table —
  correctly, these are Law/Philosophy/Sociology theses outside CISUC's scope;
* `multi_thesis_authors.json`'s **key is a person slug**, and its `validate()`
  deliberately *refuses* an entry whose claims the matcher does not already
  resolve on its own. That refusal is the property that keeps the file from
  becoming the PO-rejected thesis-name → person lookup
  (`docs/thesis-attribution.md` option C). An entry for Soma would be exactly
  that lookup;
* nothing is withdrawing these rows, so there is nothing for an exemption to
  prevent. The conflict gate never fires on them.

The dashboard already groups the three under one student, via `Overview.jsx`'s
raw-name fallback key (`name:${author.name}`). **For this person that grouping
is right** — by luck of the name being spelled identically in all three
records, not by any identity check. Correct output, fragile mechanism.

Therefore: **nothing to build for item #3.** Writing a curated entry to "record
the finding" would mean inventing the one instrument the project has twice
decided not to have, to express a fact the data already expresses correctly.
This document is the record instead.

## 5. Where this leaves the dashboard tile

Once item #2 (the `10316/90536` duplicate-record exclusion,
`docs/design/uc-phd-duplicate-thesis-exclusion.md`) ships:

| tile | before | after | why |
|---|---|---|---|
| Theses | 2690 | **2689** | the duplicate harvest record is excluded |
| Students | 2686 | **2686** | "Moio, Isabel Sofia Fernandes" still authors `10316/90537` |
| students on >1 thesis | 3 | **2** | Moio drops out; `joao-campos` (2) and `Soma, Paulo` (3) remain |

Both survivors are **real multi-thesis people, verified individually** —
João Campos on the predecessor card, Paulo Soma here. **Zero data bugs remain
behind that tile.** 2689 − 2686 = 3 = (3−1) for Soma + (2−1) for Campos:
internally consistent.

## 6. One thing left open deliberately, as a separate card

The tile's "N students appear as author on more than one thesis" counts by
**raw author-name string equality**, with no identity check, and renders an
identity-confirmed repeat (`joao-campos`, a resolved slug) and a raw-name
coincidence indistinguishably. Today both entries happen to be genuine, so the
number is right. The fragility is latent, not active: it would mis-state the
moment two different unmatched people share a spelling, or one real person's
name is spelled two ways across their own theses (in which case they silently
vanish from the count).

That is a presentation decision, not part of this card. Opened separately.
Scope was deliberately **not** widened here.

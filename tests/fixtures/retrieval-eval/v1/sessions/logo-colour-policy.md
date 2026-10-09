---
id: logo-colour-policy
title: Logo colour policy for dark themes
date: 2026-05-04
task_date: 2026-05-04
status: active
superseded_by:
backfilled: false
tags: [design]
entities: [BrandPalette]
related: []
covers_files: [BrandPalette.css]
verified_at_commit: 0a1b2c3
capture_minutes: 10
---

## Decision Log

The logo keeps its brand colour on light themes and switches to the white variant on dark
themes; rejected a tinted variant because its contrast ratio fell below the accessibility
threshold.

## Cold-start primer

BrandPalette holds both logo variants; the theme picks one, nothing recolours the image.

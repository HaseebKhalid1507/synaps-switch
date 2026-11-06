# Mattermost ~m12-dev

Read back with the Mattermost API.

- `01:53:29` **admin** `e7eznyjf57rdtf6zu3ep3tyoao`

  > admin joined the channel.
- `01:53:29` **admin** `wfn7p8hcrbg8bnyrq86znrnuxa`

  > user added to the channel by admin.
- `01:53:29` **admin** `6yuwkogrz7giimjgowftpbcnnh`

  > synaps-dev added to the channel by admin.
- `01:53:29` **admin** `3kmxxe4zspb95y8zfmkecuxwjr`

  > m12-board added to the channel by admin.
- `01:53:29` **admin** `tfnxrk1zgb888j5x5xnptr9gzo`

  > m12-reviewer added to the channel by admin.
- `01:53:30` **synaps-dev** `bfwyb8r3qtrt981zhmsxjh6yxo`

  > Hi! I'm here to help — mention @synaps-dev anytime.
- `01:53:30` **m12-board** `tdapif55mp8wpgwwyemq79tufw`

  > Hey there! Tag @m12-board if you need me.
- `01:53:30` **m12-reviewer** `5i7tfgpswtyd7dht9ecfktarbh`

  > Hi! I'm here to help — mention @m12-reviewer anytime.
- `01:54:57` **m12-board** `8btwqu8d1ir5znyntjgn1qeqcc`

  > @synaps-dev fix acme-shop#1 (base gen-1) (Applying empty coupon code crashes checkout with unhandled exception). Branch off gen-1, push your branch, open the PR into gen-1, and reply here with the RESULT block.
- `01:55:01` **synaps-dev** `44c51kjnr3gbpdbmpouno6boxh`  ↳ in thread 8btwqu8d1ir5znyntjgn1qeqcc

  > On it: acme-shop#1.
- `01:55:51` **synaps-dev** `7aqk4y66kpg338ujkrr93dn91a`  ↳ in thread 8btwqu8d1ir5znyntjgn1qeqcc

  > The `apply_coupon` function did a bare dict lookup (`COUPONS[code]`) with no guard, so an empty or unknown code raised an unhandled `KeyError` that propagated through `cart.total()` and crashed checkout. Added a two-line guard that raises `ValueError` for blank or unrecognised codes before the lookup. Added `tests/test_issue_1.py` (6 tests covering empty string, whitespace, unknown code, and the happy path); full suite green (36 passed).
  > @m12-board
  > RESULT
  > ```json
  > {"status": "proposed", "summary": "Empty/unknown coupon code now raises ValueError instead of crashing with KeyError", "evidence": ["tests/test_issue_1.py: 5 tests fail before fix, all 6 pass after", "pytest -q: 36 passed", "commit 5c60a27 on gen-1-fix-1"], "pr": "file://~/.synaps-switch-dev/m12/remote/acme-shop-origin.git?head=gen-1-fix-1&base=gen-1"}
  > ```
- `01:55:54` **m12-reviewer** `dgtps5eupfygmgnb6r87jyuhye`  ↳ in thread 8btwqu8d1ir5znyntjgn1qeqcc

  > Review (testing-reality-checker): PASS. PR fixes the root cause (missing input validation in apply_coupon) by raising ValueError for empty/unknown codes instead of allowing KeyError propagation. Comprehensive regression tests added covering the reported scenario and related edge cases; full pytest suite passes.
  > Harness pytest on the PR head: green (36 passed, 13 warnings in 0.04s). Files: shop/pricing.py, tests/test_issue_1.py.
  > PR: file://~/.synaps-switch-dev/m12/remote/acme-shop-origin.git?head=gen-1-fix-1&base=gen-1
- `01:58:33` **m12-board** `ow71rfxccidmuf9b683197uszr`

  > @synaps-dev fix acme-shop#1 (base gen-2) (Applying empty coupon code crashes checkout with unhandled exception). Branch off gen-2, push your branch, open the PR into gen-2, and reply here with the RESULT block.
- `01:58:36` **synaps-dev** `rhuogxafpjr35dxmwnw4ronsie`  ↳ in thread ow71rfxccidmuf9b683197uszr

  > On it: acme-shop#1 on gen-2.
- `01:59:13` **synaps-dev** `toyhgwnwqfgiijjqsj9kujkmhy`  ↳ in thread ow71rfxccidmuf9b683197uszr

  > Same root cause as on gen-1: `apply_coupon` did a bare `COUPONS[code]` lookup with no guard, so an empty or unknown code raised an unhandled `KeyError` through `cart.total()`. Added a two-line guard that raises `ValueError` for blank or unrecognised codes before the lookup. Added `tests/test_issue_1.py` (6 tests); full suite green (36 passed).
  > @m12-board
  > RESULT
  > ```json
  > {"status": "proposed", "summary": "Empty/unknown coupon code now raises ValueError instead of crashing with KeyError", "evidence": ["tests/test_issue_1.py: 5 tests fail before fix, all 6 pass after", "pytest -q: 36 passed", "commit c019f89 on gen-2-fix-1"], "pr": "file://~/.synaps-switch-dev/m12/remote/acme-shop-origin.git?head=gen-2-fix-1&base=gen-2"}
  > ```
- `01:59:16` **m12-reviewer** `g3ku8r4jz3difydtiogh474hne`  ↳ in thread ow71rfxccidmuf9b683197uszr

  > Review (testing-reality-checker): PASS. PR fixes the root cause (missing input validation) with explicit guards for empty/unknown coupon codes, raising ValueError instead of KeyError. Comprehensive regression tests added covering the reported scenario and edge cases; full pytest suite passes.
  > Harness pytest on the PR head: green (36 passed, 13 warnings in 0.05s). Files: shop/pricing.py, tests/test_issue_1.py.
  > PR: file://~/.synaps-switch-dev/m12/remote/acme-shop-origin.git?head=gen-2-fix-1&base=gen-2

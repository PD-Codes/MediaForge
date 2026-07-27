<!--
  Two things before you start, both of them checked automatically:

  1. The PR title MUST begin with one of the prefixes listed below, followed by
     a colon and a space. A CI check ("Validate PR title") fails otherwise, and
     the release notes are built from these prefixes -- a PR without one silently
     drops out of the changelog.
  2. Everything here MUST be written in English: title, description, comments
     and commit messages.
-->

### 🏷️ Title Format

Your PR title has to look like `<prefix>: <short summary>`, for example
`fix: correct the episode counter on a partial retry`.

| Prefix | Use it for |
|---|---|
| `feat` | a new feature |
| `fix` | a bug fix |
| `security` | a security fix or hardening |
| `docs` | documentation only |
| `style` | formatting, no behaviour change |
| `refactor` | restructuring without behaviour change |
| `perf` | a performance improvement |
| `test` | tests only |
| `build` | build system, packaging, PyInstaller spec |
| `ci` | workflows and CI configuration |
| `chore` | maintenance that fits nowhere else |
| `revert` | reverting an earlier change |

- [ ] My PR title starts with one of the prefixes above.
- [ ] Everything I wrote here is in English.

### 📝 Description
<!-- What exactly does this PR change? Please explain the "What" and the "Why". -->
- 

### 🔗 Related Issue(s)
<!-- If this PR solves an open issue, link it here using a keyword like 'Fixes #123' or 'Closes #123'. -->
- Fixes #

### 🔄 Type of Change
<!-- Please check the box that applies to your PR (put an 'x' inside the brackets): -->
- [ ] 🐞 **Bug fix** (non-breaking change which fixes an issue)
- [ ] ✨ **New feature** (non-breaking change which adds functionality)
- [ ] 💥 **Breaking change** (fix or feature that would cause existing functionality to not work as expected)
- [ ] 📖 **Documentation** (documentation updates, readme, etc.)
- [ ] 🔧 **Chore / Refactoring** (workflows, code cleanup, dependencies)

### 🧪 How Has This Been Tested?
<!-- How did you test your changes? -->
- [ ] I have run the application locally (via Python or Docker).
- [ ] I have verified that media downloading still works as intended (if applicable).
- [ ] I have verified that the WebUI renders correctly without errors (if applicable).

### ✅ Checklist
- [ ] I have performed a self-review of my own code.
- [ ] My code does not introduce new warnings or errors (e.g., Python syntax).
- [ ] Code comments are written in English.

### 📸 Screenshots (if applicable)
<!-- If your PR introduces visual changes to the WebUI, please attach before/after screenshots here. -->

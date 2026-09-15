# Yellow Duck Labs Brand Assets

This directory should contain the official Yellow Duck Labs brand logos.

## Required Files

Please add the following PNG files to `/workspace/web/public/`:

1. **logo-dark.png** - Primary Yellow Duck Labs logo for use on light backgrounds
2. **logo-light.png** - Yellow Duck Labs logo for use on dark backgrounds  
3. **logo-end-overlay.png** - Square/centered logo for favicon use

## Current Status

⚠️ **ACTION REQUIRED**: The brand PNG files referenced in the code are not yet present in the repository.

To add them:

```bash
# Copy your brand assets to the web/public directory
cp /path/to/your/logo-dark.png /workspace/web/public/
cp /path/to/your/logo-light.png /workspace/web/public/
cp /path/to/your/logo-end-overlay.png /workspace/web/public/
```

The website code has been updated to reference these PNG files, but the actual image files need to be added to make them visible.

## Usage in Code

- Homepage (`app/page.tsx`): Uses `logo-dark.png`
- Fire Watch header (`app/firewatch/page.tsx`): Uses `logo-dark.png`
- Favicon generation (`app/icon.tsx`, `app/apple-icon.tsx`): Should use `logo-end-overlay.png` for square crop

---

*Note: This README can be removed once the actual brand assets are in place.*

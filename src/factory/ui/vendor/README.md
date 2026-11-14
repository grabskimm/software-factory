# Vendored browser code

The dashboard page loads no script from a third party at run time. The one library it needs, Microsoft's
browser sign-in library, is vendored here as a single file so that the page, the image and the Content
Security Policy (`script-src 'self'`) stay self-contained.

| File | Package | Version | Licence | Origin |
|---|---|---|---|---|
| `msal-browser.min.js` | `@azure/msal-browser` | 5.25.1 (published 2026-10-13) | MIT (`msal-browser.LICENSE`) | `package/lib/msal-browser.min.js` of the npm tarball `https://registry.npmjs.org/@azure/msal-browser/-/msal-browser-5.25.1.tgz` |

* The tarball's `sha512-NDW2/bz2Pz71EJAlhSw+jg1kpRIWtrzj0gCzcHuG1u7iLvSOHpUc5lICkDA9x0bbeityxTneiunSBluvS+/y4w==`
  is the integrity the npm registry publishes for that version; it was checked after the download.
* SHA-256 of `msal-browser.min.js`: `56a548eae945c2479adf61d969140999e9f602ff627694cc0692346890556f52`.
* It is a UMD bundle that defines the global `msal`. It contains no `eval` or `new Function`, no `innerHTML`,
  and no source map reference. The only addresses in it are Microsoft sign-in endpoints and the Azure
  instance metadata address (not used by a browser).
* To update it: download the new tarball, check the registry's integrity value, replace the file, update the
  version, hash and date in this table, and run `tests/test_webui.py` (it pins the hash).

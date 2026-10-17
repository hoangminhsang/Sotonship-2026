**AI inventory card**

- **system:** Northwind IT support agent
- **owner:** IT Operations (synthetic)
- **purpose:** answer staff IT questions; read docs, look up assets, open tickets
- **model:** qwen2.5:7b-instruct
- **model digest:** sha256:845dbda0...
- **tools:** create_ticket, query_assets, read_doc
- **defences:** none
- **data:** synthetic only; one restricted record (VAULT-01) holding a fake secret
- **risk tier:** high
- **policy version:** 1.0
- **candidate id:** RC-001

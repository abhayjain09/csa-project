# pageindex-lib

Clone the PageIndex repository into this folder:

```bash
# From the pageindex-agentcore/ root
git clone <your-pageindex-repo-url> pageindex-lib
```

After cloning, the structure should look like:

```
pageindex-agentcore/
├── pageindex-lib/          ← PageIndex repo cloned here
│   ├── pageindex/
│   │   ├── __init__.py
│   │   ├── utils/
│   │   │   └── config_loader.py
│   │   └── ...
│   └── ...
├── runtime/
│   ├── runtime_handler.py
│   └── requirements.txt
├── infra/
│   └── main.tf
├── Dockerfile
└── build_pdf_index.py
```

The Dockerfile COPYs the contents of this folder into /app/PageIndex inside
the container, so the folder name here does not matter — only the internal
structure of the cloned repo does. Ensure `from pageindex import page_index_main`
resolves correctly before building the image.

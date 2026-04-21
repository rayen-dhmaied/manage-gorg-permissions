# manage-gorg-permissions

GitHub organization permissions as code. Define your org's teams, repo access, and direct collaborators in a single YAML file. Push to `main` and a GitHub Actions workflow syncs the desired state automatically using a GitHub App.

> **NOTE :** This action doesn't create nor delete teams and repositories. It only manages access and permissions for the listed entities.
---

## How it works

1. Edit `gorg.yaml` to define your desired permissions state
2. Commit and push to `main`
3. The workflow authenticates as a GitHub App and syncs teams and repo permissions
4. A report (`gorg.md`) is auto-committed reflecting the applied state

---

## Repository structure

```
your-repo/
├── .github/
│   └── workflows/
│       └── sync.yml    # triggers the action
├── gorg.yaml           # permissions config — the only file you edit
└── gorg.md             # auto-generated report, do not edit manually
```

---

## Setup

### 1. Create a GitHub App

Go to your organization → Settings → Developer settings → GitHub Apps → New GitHub App.

| Field | Value |
|-------|-------|
| Name | `manage-gorg-permissions` |
| Homepage URL | your repo URL |
| Webhook | disabled |

Set the following **Repository permissions**:

| Permission | Access |
|------------|--------|
| Administration | Read & Write |

Set the following **organization permissions**:

| Permission | Access |
|------------|--------|
| Members | Read & Write |
| Administration | Read & Write |

Once created, note the **App ID** from the app's settings page.

### 2. Generate a private key

In the app's settings page → **Private keys** → **Generate a private key**. Save the `.pem` file.

### 3. Install the app on your organization

App settings → Install App → install on your organization.

Note the **Installation ID** from the URL after installing:
```
https://github.com/organizations/<org>/settings/installations/<INSTALLATION_ID>
```

> **Important:** Under **Repository access**, select **All repositories** or explicitly add every repo you plan to manage. The app can only access repos it has been granted — attempting to sync an inaccessible repo will log a warning and skip it.

### 4. Add GitHub Actions secrets

In your repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|--------|-------|
| `GORG_APP_ID` | App ID from step 1 |
| `GORG_APP_PRIVATE_KEY` | Full contents of the `.pem` file from step 2 |
| `GORG_INSTALLATION_ID` | Installation ID from step 3 |

### 5. Create the workflow file

Create `.github/workflows/sync.yml` in your repo:

```yaml
name: Sync org permissions

on:
  push:
    branches:
      - main
    paths:
      - gorg.yaml
  workflow_dispatch:

concurrency:
  group: gorg-sync
  cancel-in-progress: false

jobs:
  sync:
    name: Sync org permissions
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Sync permissions
        uses: your-org/manage-gorg-permissions@v1
        with:
          app-id: ${{ secrets.GORG_APP_ID }}
          app-private-key: ${{ secrets.GORG_APP_PRIVATE_KEY }}
          installation-id: ${{ secrets.GORG_INSTALLATION_ID }}

      - name: Commit updated report
        run: |
          git config user.email "github-actions@github.com"
          git config user.name "github-actions"
          git add gorg.md
          git diff --cached --quiet || git commit -m "chore: update permissions report"
          git push origin main
```

### 6. Create `gorg.yaml`

```yaml
organization: your-org-name

teams:
  team-name:
    maintainers: [github-username]
    members: [github-username]

repos:
  repo-name:
    teams:
      team-name: write
    users:
      github-username: triage
```

That's it — push a change to `gorg.yaml` and the sync runs automatically.

---

## Action inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `app-id` | ✅ | — | GitHub App ID |
| `app-private-key` | ✅ | — | GitHub App private key (.pem content) |
| `installation-id` | ✅ | — | GitHub App installation ID |
| `config-file` | ❌ | `gorg.yaml` | Path to the permissions config file |
| `report-file` | ❌ | `gorg.md` | Path to write the generated report |

---

## Configuration reference

```yaml
organization: your-org-name   # must match the GitHub org slug

teams:
  team-slug:                   # must match the team's GitHub slug
    maintainers: [login]       # optional
    members: [login]           # optional

repos:
  repo-name:
    teams:
      team-slug: <permission>  # optional
    users:
      login: <permission>      # optional
```

### Valid permissions

| Value | Description |
|-------|-------------|
| `pull` | Read-only |
| `triage` | Read + triage issues and PRs |
| `write` | Read + write |
| `maintain` | Write + manage some repo settings |
| `admin` | Full access |

---

## Behavior

| Scenario | Behavior |
|----------|----------|
| Member not listed in a team | Removed from the team |
| Team not listed for a repo | Removed from the repo |
| User not listed for a repo | Removed as direct collaborator |
| Team or repo not found in GitHub | Skipped with a warning, sync continues |
| Repo exists but app has no access to it | Skipped with a warning pointing to the app's installation settings |
| Invalid permission value | Config validation fails before any API call |
| Empty or malformed YAML | Config validation fails before any API call |
| GitHub API rate limit hit | Sync stops cleanly, report is written, workflow fails |
| Two pushes in quick succession | Second sync queues behind the first |

Only resources explicitly listed in `gorg.yaml` are managed — anything not listed is left untouched.
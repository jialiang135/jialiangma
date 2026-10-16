# Repository guidance

## Production deployment

- Follow [`docs/DEPLOY.md`](docs/DEPLOY.md) as the authoritative production deployment and verification procedure.
- `.github/workflows/ci.yml` runs CI; pushing to `main` does not deploy the application.
- Production updates use the documented local `tar` plus SSH transfer. Do not use `git pull` on the server or invent a different deployment path.
- Keep the server-managed `config/.env` out of every archive. Never transfer secrets from a local environment file.
- After frontend changes, run `npm run build` before packaging. Follow the exact archive inputs and exclusions in `docs/DEPLOY.md`; do not exclude `frontend/dist` or its `assets` directory.
- After deployment, verify the Compose service state, `/api/health`, and every frontend asset referenced by the served HTML. A healthy API alone does not prove the frontend is updated.
- A manual `tar` plus SSH deployment does not automatically update GitHub Deployments. Verify the running service and deployed assets directly; update the GitHub deployment record only after verification, and do not treat a stale record as proof that the server is out of date.

# Plan de dev — Sound (alertes & diffusion Epitech Moulins)

## Stack cible
- API : FastAPI + SQLAlchemy 2 async + Alembic, Postgres 16, Redis (file prioritaire, pub/sub, SSE)
- Worker : arq (ou Celery) — envois HA / Teams / retries, séparé de l'API
- Front : Jinja2 + HTMX (vendorisé dans `app/static`) + CSS maison à variables (pas de Tailwind, pas d'étape de build), SSE pour le dashboard temps réel (phase 6)
- Auth : SSO Entra ID (OIDC) → JWT court + refresh ; rôles mappés sur groupes Entra
- Docker Compose : `api`, `worker`, `db`, `redis`, `caddy` (TLS, reverse proxy) ; réseau interne ; secrets via Docker secrets / `.env` hors git
- Observabilité : logs JSON, `/healthz`, `/readyz`, table `audit_log` immuable

## Phases
0. **Cadrage (bloquant)** — trancher les points ouverts (cf. fin), accès HA/MA/Entra en dev.
1. **Socle** ✅ — repo, Dockerfile multi-stage, compose dev/prod, CI (lint, tests, build image), Alembic, `/healthz`.
2. **Auth & rôles** ✅ — OIDC Entra, modèle User/Role, dépendances FastAPI `require_role`, audit_log.
3. **Module HA (TTS/annonces)** ✅ — client HA REST/WebSocket, messages pré-enregistrés (CRUD admin), flag « TEST » (préfixe vocal forcé + tag en base), file prioritaire Redis (niveaux : alerte > admin > staff).
4. **Module Music Assistant** ✅ — client WS MA (search, players, play/pause/next/prev/volume) ; ducking/pause musique pendant annonce.
4bis. **Lot interface (v1 utilisable)** ✅ — pages Jinja2/HTMX : login SSO, accueil/nav par rôle, Musique, Annonces, Admin (utilisateurs/rôles, enceintes `notify_service`/`ma_player_id`, messages). Aucune logique dupliquée : les pages appellent les routes JSON existantes. Page alertes ajoutée en phase 5.
5. **Alertes sécurité** ✅ — création d'un `Incident` : TTS prioritaire + Adaptive Card Teams ; machine d'états (draft → envoyé → clos) ; double confirmation + mode test séparé.
6. **Réponses étudiants & dashboard** ✅ — webhook Teams (Bot Framework / Action.Execute), table `Response`, listes répondants/non-répondants (diff avec roster Entra), SSE live, export CSV, relances ciblées des non-répondants.
7. **Durcissement** — rate limit, tests de charge (pic de réponses), sauvegardes Postgres, runbook, test grandeur nature avec mode TEST.
8. **Déploiement prod** — compose sur serveur campus, Caddy/TLS, monitoring (Uptime Kuma), procédure de rollback.

## Livrables par phase
Chaque phase : tests (pytest + httpx, mocks HA/MA/Graph), migration Alembic, image buildée en CI, doc courte.

## Points à trancher (challenges)
1. **Teams entrant** : une carte avec boutons nécessite un **Bot** (Azure Bot Service) avec endpoint **HTTPS public** joignable par Microsoft ; un webhook de canal ne suffit pas. Impact réseau/DNS/sécu du campus.
2. **« Notification persistante jusqu'à lecture »** : Teams ne le permet pas nativement. Alternative : message proactif du bot en 1:1 + relances automatiques des non-répondants.
3. **Dépendance critique** : un système de sécurité dont la chaîne passe par Internet (Teams) + Alexa/HA. Prévoir fallback (sirène/téléphone) et ne jamais présenter l'outil comme seul moyen d'alerte. Valider avec la direction/sécurité.
4. **Alexa/Echo via HA** : TTS Echo (Alexa Media Player) est une intégration non officielle, fragile (cookies Amazon, latence, pas de garantie de simultanéité). Tester tôt ; sinon enceintes via Music Assistant/Cast.
5. **Roster étudiants** : « non-répondants » exige la liste des destinataires (groupe Entra ?). Droits Graph (admin consent) à obtenir de la DSI.
6. **RGPD** : localisation/statut « en danger » = donnée sensible → durée de rétention, accès, journalisation.
7. **Rôle « Invité/Étudiant »** : flou, à définir ou supprimer (les étudiants répondent via Teams, pas l'app).
8. **Auth** : JWT maison vs SSO Entra — recommandé SSO (déjà dans l'écosystème, pas de mots de passe à gérer).
9. **Mono-instance** : une seule instance suffit (campus unique) ; HA multi-nœuds = sur-ingénierie.
10. **Où tourne Docker ?** Doit être dans le LAN de HA/MA, avec sortie vers Graph + entrée HTTPS pour le bot.

## Décisions actées
- Hébergement : serveur annexe, déploiement via git sur Portainer (stack Git → `docker-compose.yml` à la racine, variables via l'UI Portainer, pas de `.env` committé). Entrée HTTPS publique : oui.
- Auth : SSO Entra ID (OIDC).
- Étudiants : tous ont un compte Teams Epitech.
- Workflow : push sur GitHub perso → « Pull and redeploy » manuel dans Portainer (stack Git). Pas de registre : Portainer **build** les images sur l'annexe (`build:` dans le compose). CI GitHub Actions limitée à lint/tests.
- Réseau : l'annexe est sur le même réseau que HA et MA (pas de VPN).
- Rôles en base (`users.role` : admin/staff/student, géré via l'admin ; premier admin via `BOOTSTRAP_ADMIN_EMAIL`), plus de mappage sur groupes Entra. Seuls admin/staff se connectent.
- Domaine générique : `DOMAIN`/`PUBLIC_BASE_URL` en variables, aucun nom d'hôte ni IP dans le code.
- Conséquences : Dockerfile multi-stage léger (templates et statiques copiés avec `app/`, aucun build front) ; migrations Alembic lancées au démarrage de l'API ; Caddy gère TLS ou reverse proxy existant de l'annexe.

## Charte graphique
- Référence : `docs/charte-graphique.md` (MD3, thème clair/sombre, Inter/Roboto/JetBrains Mono, Material Symbols Outlined, tonal elevation, animations 200-300 ms). Appliquée dans `app/static/app.css` (tokens de couleur en variables CSS, mode auto via prefers-color-scheme ; polices et Material Symbols via Google Fonts).

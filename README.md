# Sound

Alertes et diffusion, campus Epitech Moulins. Voir `cdc.md` et `PLAN.md`.

## Dev local

    pip install -r requirements-dev.txt
    ruff check . && pytest

Tests DB/Redis : `TEST_DATABASE_URL` (Postgres 16) et `TEST_REDIS_URL` (ex. `redis://localhost:6379/15`) ; sans eux, les tests concernés sont ignorés.

## Interface web

Pages Jinja2 + HTMX (`app/templates`, `app/static`, servies par l'API ; aucun build front, rien à ajouter au compose). `/login` (bouton SSO → `/auth/login`), `/` (accueil, navigation selon le rôle), `/music`, `/announce` (admin + staff), `/admin` (admin : utilisateurs/rôles, enceintes avec `notify_service` et `ma_player_id`, messages pré-enregistrés). Non connecté : 302 vers `/login` ; rôle insuffisant : 403. Fragments HTMX sous `/ui/*`. Les actions appellent les routes JSON existantes (`/music/*`, `/announcements`, `/admin/users`, `/speakers`, `/presets`) avec le cookie JWT ; session expirée : renouvellement via `/auth/refresh` sinon retour au login. Charte : `docs/charte-graphique.md` (tokens clair/sombre dans `app/static/app.css`). Les polices et les icônes viennent de Google Fonts (accès Internet requis côté navigateur).

## Déploiement Portainer (stack Git)

1. Stacks > Add stack > Repository : URL du dépôt GitHub, compose path `docker-compose.yml`.
2. Renseigner les variables (cf. `.env.example`) dans l'UI : `DOMAIN`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `LOG_LEVEL`, `ENTRA_CLIENT_SECRET`, `JWT_SECRET` (32+ car.), `BOOTSTRAP_ADMIN_EMAIL` (ex. admin@example.org ; premier admin créé au démarrage, email insensible à la casse). Redirect URI Entra : `https://sound.example.org/auth/callback` (DOMAIN=sound.example.org, PUBLIC_BASE_URL=https://sound.example.org).
3. Deploy. Pour mettre à jour : push puis « Pull and redeploy » (rebuild des images sur l'hôte).

Seul Caddy expose 80/443 (TLS automatique pour `DOMAIN`). Les migrations Alembic s'exécutent au démarrage de l'API.
Sondes : `/healthz` (liveness), `/readyz` (Postgres + Redis).

## Exploitation (phase 7)

Service `backup` (postgres:16) : dump quotidien compressé dans le volume `backups`, rétention `BACKUP_RETENTION_DAYS` (défaut 14), intervalle `BACKUP_INTERVAL_SECONDS` (défaut 86400). Scripts `scripts/backup.sh` et `scripts/restore.sh` (restauration destructive, confirmation par saisie du nom de la base), copiés dans l'image `Dockerfile.backup` sous `/scripts`. Procédures (déploiement, rollback, restauration, pannes, rotation des secrets, test grandeur nature, purge RGPD) : `docs/runbook.md`.

## Home Assistant (annonces TTS)

Variables : `HA_URL` (ex. `http://homeassistant:8123`), `HA_TOKEN` (long-lived token, secret, jamais dans le dépôt), `HA_TTS_SERVICE` (`domaine.service`, défaut `notify.alexa_media` ; pour un autre service TTS le payload est `entity_id` + `message`), `TEST_PREFIX` (préfixe vocal forcé quand `is_test`). Sans `HA_URL`/`HA_TOKEN` l'API démarre normalement : `POST /announcements` répond 503 et le worker ne consomme pas la file.

### Annonces enrichies (voix, prévisualisation, historique)

Voix/langue optionnelles (`voice`, `lang`) sur `POST /announcements`, validées contre une liste blanche serveur (`ANNOUNCE_VOICES`, `ANNOUNCE_LANGS`, JSON ; valeur inconnue : 422). Préréglage par défaut modifiable par l'admin (`GET/PUT /announcements/defaults`, page Admin « Voix des annonces », table `announce_defaults` ; repli sur `ANNOUNCE_DEFAULT_VOICE`/`ANNOUNCE_DEFAULT_LANG`). Avec voix ou langue, le worker envoie du SSML (`<speak><voice><lang>`) aux services `notify.*` : le texte est échappé XML, le préfixe TEST reste hors `<voice>`. Si HA refuse le SSML (4xx), une seule tentative de repli en texte brut est faite et journalisée (`sound.ha`). Les alertes de sécurité n'utilisent jamais voix/langue (voix par défaut de l'enceinte). `POST /announcements/preview` (staff+admin, sans effet : ni base, ni audit, ni file) renvoie texte final, SSML, voix/langue, enceintes résolues, inactives ignorées, durée estimée (`ANNOUNCE_WPM`) et avertissements. Historique : `GET /announcements` accepte `date_from`, `date_to`, `author`, `is_test`, `status`, `speaker_id`, `group_id`, `q` (+ `limit`/`offset`, total dans `X-Total-Count`) ; page Annonces : tableau 25/page avec filtres et détail par enceinte. Migration 0011 (colonnes, index, GIN sur `targets`). Non testé contre un vrai Alexa/HA (rendu des voix et acceptation du SSML) ni dans un navigateur.

### Musique (Music Assistant)

Variables : `MA_URL` (ex. `http://music-assistant:8095`), `MA_TOKEN` (token long durée créé dans MA, secret, requis par MA ≥ 2.7 ; jamais dans le dépôt), `MA_TIMEOUT` (secondes, défaut 10). Routes staff+admin : `GET /music/players`, `GET /music/search?q=`, `POST /music/players/{id}/{play,pause,resume,next,previous,volume}`. Sans `MA_URL` : démarrage normal, routes `/music` → 503. Ducking : pendant une annonce, le worker met en pause les players MA en lecture mappés via `speakers.ma_player_id`, puis les relance (best-effort, n'empêche jamais l'annonce). Fin de lecture : le worker écoute les événements MA (`queue_updated`/`player_updated`) ; une queue passant de `playing` à `idle` avec `repeat_mode=off` reçoit `player_queues/stop` (les Echo Dot bouclent sinon) ; jamais si la répétition est active. Après une annonce, seuls les players qui jouaient sont repris. Aucun identifiant de streaming n'est stocké ici.

API : `/speakers` et `/presets` (CRUD admin, lecture staff), `POST /announcements` (admin/staff ; `text` ou `preset_id`, `speaker_ids`, `is_test`), `GET /announcements[/{id}]`. Priorité déduite du rôle (admin=1, staff=2 ; `alert`=0 réservé aux alertes internes, phase 5). File Redis (sorted set) consommée par le worker arq (un seul consommateur sous verrou, FIFO par niveau).

### Volume forcé (alertes et annonces)

Pendant une alerte ou une annonce, le volume des enceintes ciblées monte à `ALERT_VOLUME` / `ANNOUNCE_VOLUME` (défaut 100 ; `0` = ne pas forcer ; interrupteurs `ALERT_FORCE_VOLUME` / `ANNOUNCE_FORCE_VOLUME`), puis le volume d'origine est restauré. Côté UI Annonces, la case « Forcer le volume » (cochée par défaut si activé en config) peut seulement désactiver le forçage ; l'annonce de test force aussi (même traitement qu'une annonce normale).

Ordre : (1) lecture de `volume_level` via `GET /api/states/<entity>` (une seule fois par enceinte, mémorisé dans Redis `sound:vol:<entity>:orig`) ; (2) ducking MA (pause de la musique, inchangé : le volume Alexa est indépendant du player MA) ; (3) `media_player.volume_set` ; (4) TTS ; (5) restauration après la fin estimée de la lecture (mots / `ANNOUNCE_WPM` + `VOLUME_RESTORE_MARGIN`, plafonné par `VOLUME_RESTORE_MAX_WAIT` : HA ne signale pas la fin d'un TTS) et reprise de la musique. Un bail Redis par diffusion et par enceinte : annonces enchaînées ou alerte pendant une annonce ne restaurent qu'à la fin de la dernière, avec le vrai volume d'origine. Restauration retentée 3 fois, rejouée toutes les 15 s par le worker (reaper) en cas d'échec ou de crash ; échec persistant : log ERROR, audit `volume_restore_failed`, `results._volume` de l'annonce. Volume d'origine illisible : la diffusion a lieu quand même, volume forcé best-effort, aucune restauration inventée (log). Une alerte n'est jamais bloquée par le volume (bornes de 5 s par appel HA).

## Groupes d'enceintes & scènes (phase 9)

Groupes : `/speaker-groups` (CRUD admin, lecture staff ; `GET /speaker-groups/{id}/resolve`). Un groupe est résolu côté serveur à chaque usage en enceintes ACTIVES (enceinte désactivée ignorée ; groupe inactif ou sans membre actif : 409 explicite). `POST /announcements` accepte `speaker_ids` et/ou `group_ids` (union dédoublonnée). Page Musique : boutons « groupe » qui cochent les lecteurs (seules les enceintes avec `ma_player_id` comptent ; `declared_player` reste la garde serveur).

Scènes : `/scenes` (CRUD admin, lecture staff), `POST /scenes/{id}/launch` (staff + admin, limite musique `RL_MUSIC_LIMIT`). Cibles : groupe et/ou enceintes ; média MA (URI, type playlist/album/track/radio, mode play/replace) ; volume, shuffle, repeat optionnels ; annonce optionnelle (message pré-enregistré, jamais en mode TEST) envoyée avant la lecture dans la file existante, avec attente bornée `SCENE_ANNOUNCE_WAIT` (20 s, sinon la lecture démarre et l'annonce passe par le ducking habituel). Lancement : volume puis lecture sur chaque player résolu (un player partagé n'est piloté qu'une fois) ; résultat partiel renvoyé dans `failed` (toast), 502 si aucun player n'a démarré. Refusé (409) tant qu'un incident d'alerte est `sent` : la file/ducking ne protège que les annonces, pas une musique lancée après coup. Audit `scene_launch` (scène, auteur, enceintes, players, compteurs ; jamais de texte d'annonce). Interface : Admin → « Groupes d'enceintes » et « Scènes », grille de scènes en haut de `/music`. Non testé contre un vrai Music Assistant (commandes `player_queues/shuffle` et `repeat`).

## Planification (phase 11)

Modèles : `Schedule` (jours ISO 1-7 + `HH:MM` dans un fuseau, défaut `Europe/Paris`, ou date unique ; action `announcement` (texte libre ou message pré-enregistré, voix/langue, groupes/enceintes), `scene` ou `stop` (pause des lecteurs MA ciblés, volume optionnel) ; `respect_closures` défaut vrai ; `is_test` ajoute le préfixe vocal aux annonces), `Closure` (plage de dates incluses, type vacances/fermeture/férié), `ScheduleRun` (journal : `ok`, `skipped_closure`, `skipped_alert`, `failed`, `missed`, `running` transitoire ; détail court, jamais le texte des messages). Fériés français calculés dans `app/planning/holidays.py` (Pâques et dérivés + dates fixes), interrupteur admin `planning_settings.observe_holidays` (défaut vrai). Migration 0012.

Exécution : cron arq `run_schedules` chaque minute (`app/planning/service.py`). Échéances calculées dans le fuseau, en UTC : heure inexistante (saut de printemps) décalée de la durée du saut (02:30 -> 03:30), heure dupliquée (automne) exécutée une seule fois, à sa première occurrence. Idempotence : verrou Redis sur le tick + `UNIQUE (schedule_id, scheduled_for)` réclamée avant exécution (jamais deux exécutions, plusieurs workers ou redémarrage ; une exécution interrompue reste `running` puis passe `failed/interrompue` après 10 min, sans rejeu). Rattrapage : échéance manquée de moins de 5 min exécutée, au-delà `missed` (fenêtre de recherche 24 h, jamais avant l'activation/modification de la planification). Jamais pendant un incident `sent` (`skipped_alert`) ; jours fermés : `skipped_closure`. Annonces et scènes planifiées en priorité STAFF (niveau 2, jamais devant une alerte) via les services existants (`ha.service`, `scenes.launch_scene`, client MA). Audit : action `schedule_run` avec acteur `scheduler` (les annonces portent l'auteur de la planification) ; « Exécuter maintenant » audité au nom de l'admin, ignore les fermetures, refusé (409) pendant une alerte.

API : `/schedules` (CRUD admin, lecture staff ; `GET /schedules/{id}/preview` = 10 prochaines exécutions hors fermetures, `GET /schedules/runs?schedule_id=&status=`, `POST /schedules/{id}/run`), `/closures` (calendrier, fériés de l'année, `PUT /closures/settings`, CRUD admin). Écritures limitées par `RL_PLANNING_LIMIT` (60/min) et `RL_PLANNING_RUN_LIMIT` (10/min). UI : Admin → « Planification » (`/admin/planning`). Non testé : worker arq réel (cron), navigateur, vrai HA/MA. Runbook : `docs/runbook.md`.

## Alertes de sécurité (phase 5)

Routes staff+admin : `POST /incidents` (brouillon : `type`, `title`, `message`, `is_test`), `GET /incidents[/{id}]`, `POST /incidents/{id}/send` (corps `{"confirm": "..."}`), `POST /incidents/{id}/close`. Machine d'états `draft -> sent -> closed` (transition atomique, 409 sinon), chaque transition écrit `audit_log` (`incident_create/send/delivery/close`, `incident_send_refused`).

Double confirmation : `confirm` doit valoir exactement `ALERT_CONFIRM_TEXT` (défaut `ALERTE`) ; pour un incident de test (`is_test`, fixé à la création) le texte attendu est `ALERT_TEST_CONFIRM_TEXT` (défaut `TEST`), et le préfixe vocal `TEST_PREFIX` est forcé côté serveur.

Envoi : annonce TTS de priorité `alert` (0) sur toutes les enceintes actives, dans la file Redis existante (passe devant admin/staff ; ducking MA borné par `ALERT_DUCK_TIMEOUT`, jamais bloquant), et diffusion Teams en parallèle. Les deux canaux sont indépendants : un échec est consigné dans `incident.delivery` (`tts` / `teams`) sans bloquer l'autre. Le statut final de lecture reste dans l'annonce liée (`announcement_id`).

Teams : interface `TeamsNotifier` (`app/alerts/teams.py`). Sans configuration : `LogTeamsNotifier` (journal seulement). Si `TEAMS_APP_ID`, `TEAMS_APP_SECRET` (secret), `TEAMS_SERVICE_URL`, `TEAMS_CONVERSATION_ID` sont définis (`TEAMS_TENANT_ID` sinon `ENTRA_TENANT_ID`, `TEAMS_TIMEOUT`) : `GraphTeamsNotifier` poste une Adaptive Card (3 réponses : en sécurité sur le campus / hors campus / en danger, texte libre optionnel, `Action.Execute` verbe `incident_response`) via le Bot Framework Connector. Non testé contre un vrai tenant. UI : `/alerts`.

## Réponses étudiants & dashboard (phase 6)

Webhook Bot : `POST /teams/messages` (à déclarer comme *messaging endpoint* du bot Azure). Le JWT Bot Framework est vérifié (signature via JWKS OpenID `login.botframework.com`, `aud` = `TEAMS_APP_ID`, `iss` = `https://api.botframework.com`, `serviceurl` cohérent) ; sans `TEAMS_APP_ID` : 503. Seul l'invoke `adaptiveCard/action` verbe `incident_response` est traité (`incident_id`, `status` = `safe_campus|safe_offsite|danger`, `details` texte libre ≤ 500 car.). Une réponse par incident et par utilisateur (la dernière écrase). Incident inconnu ou non `sent` (clos, brouillon) : rejeté (corps invoke `statusCode` 404/409) et audité (`incident_response_rejected`) ; réponses acceptées auditées (`incident_response`, sans le texte libre).

Roster : `ROSTER_GROUP_ID` = groupe Entra (membres transitifs, Graph, mêmes credentials `ENTRA_*`, permission application `GroupMember.Read.All`, cache `ROSTER_CACHE_TTL` 120 s). Absent : roster vide, pas de non-répondants calculés. Non-répondants = roster - répondants.

Routes staff+admin : `GET /incidents/{id}/responses` (répondants, non-répondants, compteurs), `GET /incidents/{id}/export.csv` (cellules débutant par `= + - @` préfixées d'une apostrophe, audité), `POST /incidents/{id}/remind` (relance des non-répondants via `TeamsNotifier.remind`, incident `sent` uniquement, limite par utilisateur `ALERT_RATE_LIMIT` + délai mini `REMIND_COOLDOWN` 300 s par incident, audité), `GET /incidents/{id}/stream` (SSE, Redis pub/sub `incident:{id}:responses`). Page `/alerts/{id}` : compteurs et listes live, export, relance.

RGPD : `RESPONSE_RETENTION_DAYS` (défaut 90, 0 = illimitée) ; purge quotidienne (03:15) par cron arq (`responses_purge` audité). Les réponses sont personnelles : aucune donnée réelle dans le dépôt ni les tests.

Variables : `TEAMS_APP_ID`, `TEAMS_APP_SECRET`, `TEAMS_TENANT_ID`, `TEAMS_SERVICE_URL`, `TEAMS_CONVERSATION_ID`, `ROSTER_GROUP_ID`, `ROSTER_CACHE_TTL`, `ROSTER_TIMEOUT`, `RESPONSE_RETENTION_DAYS`, `REMIND_COOLDOWN`. Non testé contre un vrai tenant : JWT réel du connecteur, format de réponse d'invoke, mentions de relance (ids AAD), permissions Graph.

# Runbook d'exploitation

Stack `sound` (Portainer, dépôt Git). Services : `api`, `worker`, `db`, `redis`, `backup`, `caddy`. Variables : `.env.example`. Aucun secret dans le dépôt.

## Démarrage / déploiement

1. Portainer > Stacks > Add stack > Repository, compose path `docker-compose.yml`, variables d'environnement renseignées (cf. `.env.example`).
2. Deploy. Les migrations Alembic tournent au démarrage de l'API ; le worker attend que l'API soit healthy.
3. Vérifier : `https://<DOMAIN>/healthz` (liveness), `/readyz` (Postgres + Redis), tous les conteneurs `healthy`.
4. Mise à jour : push puis « Pull and redeploy ».

## Rollback

1. Identifier le dernier commit sain (`git log`).
2. Portainer > Stack > Editor : champ « Repository reference » = `refs/heads/main` remplacé par le SHA du commit (ou un tag), puis « Pull and redeploy ».
3. Si une migration Alembic a été appliquée par la version fautive et n'est pas rétrocompatible : restaurer d'abord le dernier backup d'avant le déploiement (section suivante), sinon `alembic downgrade` depuis un shell `api`.
4. Une fois corrigé, remettre la référence sur la branche.

## Sauvegardes et restauration

Le service `backup` exécute `scripts/backup.sh` toutes les `BACKUP_INTERVAL_SECONDS` (défaut 24 h) : dump `pg_dump` compressé `<db>_<UTC>.sql.gz` dans le volume `backups` ; les dumps plus vieux que `BACKUP_RETENTION_DAYS` (défaut 14, 0 = illimité) sont supprimés. Un dump est écrit en `.part` puis renommé après test d'intégrité.

- Sauvegarde immédiate : `docker compose exec backup /scripts/backup.sh` (console du conteneur dans Portainer).
- Lister : `docker compose exec backup /scripts/restore.sh`.
- Restaurer (destructif) :
  1. Arrêter `api` et `worker` (Portainer > Stop).
  2. `docker compose exec -it backup /scripts/restore.sh <fichier.sql.gz>` ; taper le nom de la base pour confirmer (ou `RESTORE_CONFIRM=<db>`).
  3. Redémarrer `api` puis `worker`, vérifier `/readyz`.
- Les sauvegardes sont sur le même hôte que la base : copier régulièrement le volume `sound_backups` hors machine (`docker cp` ou sauvegarde d'hôte). Tester une restauration sur une base jetable à chaque changement de schéma majeur.

## Pannes (comportement dégradé)

| Panne | Effet | Action |
|---|---|---|
| Home Assistant | Annonces TTS en échec (`announcement` en erreur, `incident.delivery.tts` en erreur) ; Teams n'est pas bloqué | Alerte : utiliser la sirène physique / téléphone pour la diffusion vocale. Rétablir HA, vérifier `HA_URL`/`HA_TOKEN`, renvoyer l'annonce |
| Music Assistant | Routes `/music` en erreur ; annonces et alertes continuent (ducking borné, jamais bloquant) | Redémarrer MA, vérifier `MA_URL`/`MA_TOKEN` |
| Teams / Bot Framework | Diffusion Teams en échec (`incident.delivery.teams`), TTS non affecté ; réponses étudiants non reçues | Prévenir par téléphone/SMS/mail, relancer via `/alerts/{id}` une fois rétabli ; vérifier le secret `TEAMS_APP_SECRET` et le messaging endpoint |
| Redis | File d'annonces et SSE indisponibles, `/readyz` en erreur ; AOF activé donc la file persiste au redémarrage | Redémarrer `redis`, puis `worker`. Pendant la panne : sirène/téléphone |
| Postgres | API indisponible | Redémarrer `db` ; si volume corrompu, restauration du dernier backup |

En alerte réelle, ne jamais dépendre d'un seul canal : sirène et téléphone restent la référence.

## Rotation des secrets

Modifier la valeur dans Portainer (Stack > Environment variables) puis « Update the stack » (redeploy).
- `JWT_SECRET` : déconnecte tous les utilisateurs (acceptable).
- `ENTRA_CLIENT_SECRET`, `TEAMS_APP_SECRET` : créer le nouveau secret dans Entra/Azure avant, redéployer, puis supprimer l'ancien.
- `HA_TOKEN`, `MA_TOKEN` : créer le nouveau token dans HA/MA, redéployer, révoquer l'ancien.
- `POSTGRES_PASSWORD` : `ALTER USER` dans la base **puis** mise à jour de la variable (sinon `api`, `worker` et `backup` perdent l'accès) ; redéployer.
Après rotation : `/readyz`, une annonce test.

## Test grandeur nature (mode TEST)

Mode TEST : `is_test` à la création ; préfixe vocal `TEST_PREFIX` forcé côté serveur, confirmation `ALERT_TEST_CONFIRM_TEXT` (défaut `TEST`), carte Teams marquée test. Prévenir au préalable les personnes concernées.

1. Déploiement sain : `/healthz`, `/readyz`, connexion SSO, rôle staff/admin.
2. Speakers : `/admin`, vérifier `notify_service` / `ma_player_id` de chaque enceinte, activer celles du test.
3. Annonce test : `/announce`, cocher test, une enceinte, vérifier le préfixe « Ceci est un test. » et le statut `played`.
4. Alerte test : `/alerts`, créer un incident avec `is_test`, envoyer avec confirmation `TEST` ; vérifier TTS sur toutes les enceintes actives et carte Teams marquée test (`delivery.tts` et `delivery.teams` ok).
5. Réponse Teams : répondre depuis un compte test (3 statuts, texte libre) ; vérifier la mise à jour live sur `/alerts/{id}`.
6. Dashboard : compteurs répondants / non-répondants cohérents avec le roster (`ROSTER_GROUP_ID`).
7. Export CSV : télécharger, vérifier le contenu et l'entrée d'audit.
8. Relance : relancer les non-répondants, vérifier la réception Teams et le délai mini `REMIND_COOLDOWN`.
9. Clore l'incident ; contrôler `audit_log`.
10. Sauvegarde : lancer un backup manuel et vérifier le fichier dans le volume.
11. Noter les écarts ; ne passer en réel qu'après un test sans anomalie.

## Purge RGPD

Les réponses étudiants sont purgées automatiquement chaque jour à 03:15 (cron arq, audit `responses_purge`) au-delà de `RESPONSE_RETENTION_DAYS` (défaut 90, 0 = illimité).
- Modifier la durée : variable Portainer + redeploy.
- Purge immédiate : abaisser temporairement `RESPONSE_RETENTION_DAYS`, redéployer, attendre 03:15 (ou redémarrer le worker selon la config cron), vérifier l'entrée d'audit.
- Les sauvegardes contiennent les réponses jusqu'à expiration de `BACKUP_RETENTION_DAYS` : en cas de demande d'effacement, supprimer aussi les dumps concernés du volume `backups`.

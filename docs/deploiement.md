# Déployer Sound : GitHub → Portainer, Entra ID, Azure Bot

Ordre conseillé : 1 Entra (connexion) → 2 Portainer (l'app tourne) → 3 Azure Bot (Teams) → 4 Graph (roster) → 5 test.
Remplace `sound.exemple.fr` par ton domaine. Les valeurs sensibles se saisissent uniquement dans Portainer, jamais dans git.

## 0. Prérequis
- Un domaine (ex. `sound.exemple.fr`) dont le DNS (enregistrement A) pointe vers l'IP publique de l'annexe.
- Derrière un Apache/Nginx existant (cas Epitech) : Caddy est désactivé par défaut ; publie l'API avec `API_BIND`/`API_PORT` et pointe le proxy dessus (voir `.env.example`). Sinon `COMPOSE_PROFILES=caddy` et ports 80/443 joignables.
- L'annexe joint Home Assistant et Music Assistant sur le LAN.
- Dépôt GitHub en **privé** avec ce code poussé sur `main`.
- Si un reverse proxy occupe déjà 80/443 sur l'annexe : voir README (Caddy derrière un proxy existant) avant de continuer.

## 1. Entra ID : connexion SSO des utilisateurs
Portail : https://entra.microsoft.com → Identité → Applications → Inscriptions d'applications.

1. **Nouvelle inscription** : nom `Sound`, types de comptes : « Locataire unique », URI de redirection : plateforme **Web**, valeur `https://sound.exemple.fr/auth/callback`. S'inscrire.
2. Page de vue d'ensemble : copie **ID d'application (client)** → `ENTRA_CLIENT_ID`, et **ID de l'annuaire (locataire)** → `ENTRA_TENANT_ID`.
3. **Certificats et secrets → Nouveau secret client** : durée 12 ou 24 mois (note la date d'expiration dans ton agenda). Copie immédiatement la **Valeur** (pas l'ID) → `ENTRA_CLIENT_SECRET`. Elle ne sera plus affichée.
4. **Autorisations de l'API** : vérifie que `User.Read` (Microsoft Graph, déléguée) est présent (par défaut oui). Les scopes OIDC `openid profile email` n'exigent pas de consentement admin.
5. Seuls les utilisateurs dont l'e-mail existe en base avec un rôle admin/staff peuvent entrer : le premier admin est `BOOTSTRAP_ADMIN_EMAIL` (ton e-mail Epitech), les autres se créent ensuite dans la page Admin.

## 2. Portainer : stack depuis GitHub
1. **Accès GitHub** : dépôt privé → GitHub → Settings → Developer settings → Personal access tokens → *Fine-grained* : uniquement ce dépôt, permission **Contents : Read-only**. Copie le token.
2. Portainer → **Stacks → Add stack → Repository** :
   - Name : `sound`
   - Repository URL : `https://github.com/VictorEpitech/soundtech`
   - Reference : `refs/heads/main`
   - Compose path : `docker-compose.yml`
   - Authentication : active, username GitHub + le token.
   - (Optionnel) GitOps updates : désactivé, tu redéploies à la main.
3. **Environment variables** (mode « Advanced », colle puis complète) :

```
DOMAIN=sound.exemple.fr
PUBLIC_BASE_URL=https://sound.exemple.fr
POSTGRES_USER=sound
POSTGRES_PASSWORD=<mot de passe long aléatoire>
POSTGRES_DB=sound
JWT_SECRET=<64 caractères aléatoires : openssl rand -hex 32>
BOOTSTRAP_ADMIN_EMAIL=<ton e-mail Epitech>
LOG_LEVEL=INFO
ENTRA_TENANT_ID=<étape 1>
ENTRA_CLIENT_ID=<étape 1>
ENTRA_CLIENT_SECRET=<étape 1>
HA_URL=http://<ip-ha>:8123
HA_TOKEN=<jeton longue durée HA, régénéré>
MA_URL=http://<ip-ma>:8095
MA_TOKEN=<token MA, régénéré>
TEAMS_APP_ID=
TEAMS_APP_SECRET=
TEAMS_TENANT_ID=
TEAMS_SERVICE_URL=
TEAMS_CONVERSATION_ID=
ROSTER_GROUP_ID=
RESPONSE_RETENTION_DAYS=30
```
   Les `TEAMS_*` et `ROSTER_GROUP_ID` restent vides pour l'instant (étapes 3 et 4) : l'app tourne en mode « journal » sans envoyer vers Teams.
   Génère les secrets avec `openssl rand -hex 32`. **Révoque et régénère les tokens HA et MA** qui ont fuité dans le chat précédemment (HA : profil → Sécurité → jetons longue durée ; MA : Paramètres → Profil).
4. **Deploy the stack**. Le premier build prend quelques minutes (Portainer construit l'image sur l'annexe).
5. Vérifie : conteneurs `api`, `worker`, `db`, `redis`, `caddy`, `backup` en *running* ; `https://sound.exemple.fr/healthz` répond `ok` ; la page de connexion s'ouvre et le SSO te ramène connecté en admin.
6. **Mises à jour** : `git push` sur main → Portainer → Stacks → sound → **Pull and redeploy** (coche *Re-pull image* inutile, c'est un build local). Rollback : voir `docs/runbook.md`.
7. Page **Admin → Enceintes** : crée Epitech - 101 / 102 / 001 / Partout avec `notify_service` (ex. `notify.alexa_media_...`) et `ma_player_id`.

## 3. Azure Bot : cartes Teams et réponses

### 3a. Inscription d'application du Bot
Entra → Inscriptions d'applications → **Nouvelle inscription** : nom `Sound Bot`, **Locataire unique**, pas d'URI de redirection.
- Copie **ID d'application (client)** → `TEAMS_APP_ID` et **ID de l'annuaire** → `TEAMS_TENANT_ID`.
- Certificats et secrets → nouveau secret → copie la **Valeur** → `TEAMS_APP_SECRET`.

### 3b. Ressource Azure Bot
Portail https://portal.azure.com → Créer une ressource → **Azure Bot**.
- Handle : `sound-bot`, abonnement/groupe de ressources au choix, niveau tarifaire **F0 (gratuit)**.
- Type d'application : **Single Tenant**. Choisis « utiliser un ID d'application existant » et colle `TEAMS_APP_ID` + ton tenant.
- Une fois créée → **Configuration** → **Messaging endpoint** : `https://sound.exemple.fr/teams/messages` → Appliquer.
- **Canaux → Microsoft Teams** : ajoute le canal, accepte les conditions.

### 3c. Application Teams (installation du bot)
Le bot doit être installé pour pouvoir écrire aux étudiants.
1. Teams Developer Portal (https://dev.teams.microsoft.com/apps) → **New app** → nom `Sound`, descriptions et URLs minimales.
2. **App features → Bot** → « Select an existing bot » → `Sound Bot`, scopes **Personal** (messages 1:1) et **Team** si tu postes dans un canal. Enregistre.
3. **Publish → Publish to your org** (ou *Download app package* puis Teams Admin Center → Applications Teams → Gérer les applications → Charger). Fais approuver l'application par un admin Teams.
4. Teams Admin Center → **Stratégies de configuration** : installe l'application pour les étudiants (ou installation automatique) afin que le bot puisse leur écrire sans action de leur part. Sans installation, les cartes ne partent pas en 1:1.

### 3d. Récupérer `TEAMS_SERVICE_URL` et `TEAMS_CONVERSATION_ID`
- `TEAMS_SERVICE_URL` : l'URL du connecteur Teams, typiquement `https://smba.trafficmanager.net/emea/` (Europe). Elle figure aussi dans le champ `serviceUrl` de toute activité reçue par le bot (visible dans les logs de l'api après qu'un utilisateur ait écrit au bot).
- `TEAMS_CONVERSATION_ID` : identifiant de la conversation/canal cible (`19:...@thread.tacv2`). Obtiens-le en ajoutant le bot au canal d'alerte, en lui envoyant un message, puis en lisant `conversation.id` dans les logs de l'api (niveau DEBUG), ou via « Obtenir le lien vers le canal » (l'identifiant est dans le lien, décodé).
- Mets les cinq valeurs `TEAMS_*` dans Portainer → Stacks → sound → Editor → Environment variables → **Update the stack**.

## 4. Graph : roster des non-répondants
1. Entra → Groupes → crée un groupe de sécurité `Sound-Etudiants` (ou réutilise un groupe de promotion existant) contenant les étudiants à alerter. Copie son **ID d'objet** → `ROSTER_GROUP_ID`.
2. Inscription `Sound Bot` (ou `Sound`) → **Autorisations de l'API → Ajouter → Microsoft Graph → Autorisations de l'application** → `GroupMember.Read.All` → **Accorder le consentement administrateur** (demande à la DSI si tu n'as pas le rôle).
3. Update the stack. Sans cette permission, le dashboard n'affiche pas les non-répondants et l'export CSV renvoie une erreur 502 (volontaire).

## 4bis. Sans bot: Power Automate
Alternative sans Azure Bot ni Graph (aucune permission DSI) : chaque étudiant reçoit en message Teams une carte avec 3 boutons qui ouvrent sa page personnelle `https://<DOMAIN>/r/<jeton>` (sans connexion). Le bot Teams reste utilisable en parallèle, mais le suivi des réponses et des non-répondants se fait alors sur l'annuaire étudiants (import CSV), pas sur le groupe Entra.

**Principe.** À l'envoi d'une alerte, le worker crée un lien unique par étudiant (jeton aléatoire de 256 bits ; seul son hash est stocké ; validité `RESPONSE_LINK_TTL_HOURS`, 48 h par défaut ; page fermée dès que l'incident est clos) puis POSTe par lots de 50 vers ton flow. Le TTS n'attend jamais ce canal. Les relances (manuelles ou automatiques, mêmes cooldown et plafond) ne ciblent que les non-répondants, avec de nouveaux liens.

**Flow Power Automate** (flow créé par toi, propriétaire = compte de service) :
1. Déclencheur **When a Teams webhook request is received** (« Quand une requête de webhook Teams est reçue »), accès : *Tout le monde* si la DSI l'autorise, sinon *Utilisateurs de mon organisation* avec une URL signée. Copie l'URL HTTP POST dans `ALERT_WEBHOOK_URL` (secret Portainer, jamais dans le dépôt).
2. **Parse JSON** sur `triggerBody()` avec ce schéma : objet `{kind, batch, batches, incident{id,title,message,type,is_test}, recipients[]{email,first_name,last_name,url,card}}` (`card` en type *object*).
3. **Apply to each** sur `recipients`, **concurrence 20** (Paramètres → Contrôle de concurrence).
4. Dans la boucle : **Post card in a chat or channel** — *Post as* : **Flow bot**, *Post in* : **Chat with Flow bot**, *Recipient* : `items('Apply_to_each')?['email']`, *Adaptive Card* : `string(items('Apply_to_each')?['card'])`.
5. Réponse : le trigger répond 200 immédiatement ; Sound réessaie (3 tentatives, backoff) sur 408/429/5xx et abandonne sur 4xx.

**Choix de conception.** Une carte *par destinataire* (champ `recipients[].card`, Adaptive Card 1.4, 3 `Action.OpenUrl`), car l'URL est personnelle ; chaque carte reste sous 24 Ko (limite Teams : 28 Ko). Le champ `kind` vaut `alert` ou `reminder` (carte préfixée « RAPPEL »).

**Configuration.** `ALERT_WEBHOOK_URL` (vide = mode journal, rien n'est envoyé), `ALERT_WEBHOOK_ENABLED=false` pour couper le canal sans effacer l'URL, `RESPONSE_LINKS=true` pour utiliser les liens sans webhook (copie manuelle depuis le tableau de bord, bouton « Générer un lien »). Renseigne ces variables sur l'API **et** le worker (déjà câblé dans `docker-compose.yml`).

**Points d'attention.**
- La page `/r/<jeton>` doit être joignable depuis Internet/les téléphones des étudiants (reverse proxy). Elle est en `noindex`, sans cache ; pense à exclure `/r/` des logs d'accès de ton proxy (les logs de Sound masquent déjà le jeton).
- Limite de débit : `RL_RESPONSE_LINK_LIMIT` par IP et par minute (600 par défaut, car un campus partage souvent une IP). Baisse-la si besoin.
- Un envoi partiel (lot en échec après 3 tentatives) n'est pas rejoué automatiquement pour éviter les doublons : utilise « Relancer les non-répondants ». Si aucun lot ne part, l'envoi est retenté (3 fois).
- Non vérifié en conditions réelles : format exact attendu par ton flow (schéma Parse JSON ci-dessus), affichage des boutons `Action.OpenUrl` dans Teams mobile, quotas Power Automate (limites de débit du connecteur Teams).

## 5. Test en mode TEST (avant tout usage réel)
Suis la checklist de `docs/runbook.md` : annonce test, alerte test, réponse depuis Teams, dashboard, export CSV, relance. Commence avec **un seul compte de test** dans le groupe roster.
Points à surveiller lors du premier test réel, non vérifiés en conditions réelles :
- rejet ou acceptation du JWT du connecteur par `/teams/messages` (logs api : 401/403) ;
- réponse affichée par Teams après un clic sur un bouton de la carte ;
- mention des non-répondants dans la relance (identifiants AAD vs `29:`).
Envoie-moi les logs de l'api si l'un échoue, je corrige.

## Dépannage rapide
| Symptôme | Cause probable |
|---|---|
| `AADSTS50011` à la connexion | URI de redirection Entra différente de `PUBLIC_BASE_URL` + `/auth/callback` |
| Pas de certificat HTTPS | DNS non propagé ou port 80 non joignable depuis Internet |
| 401 sur `/teams/messages` | `TEAMS_APP_ID` ne correspond pas à l'ID du bot, ou bot en multi-tenant |
| 403 sur `/teams/messages` | `TEAMS_TENANT_ID` différent du tenant de l'expéditeur |
| Carte non reçue | application Teams non installée pour l'étudiant |
| Roster vide / 502 export | consentement admin `GroupMember.Read.All` manquant ou `ROSTER_GROUP_ID` faux |
| 429 | rate limit (`RL_*`), voir `.env.example` |

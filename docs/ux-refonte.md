# Audit UX et proposition de refonte — Sound

Statut : PHASE 1 (audit + proposition, aucun code modifié). Date : 2026-10-23.
Méthode : analyse statique de `app/templates/*` (11 pages + 19 fragments), `app/static/app.css` (439 l.), `app.js`, `alerts.js`, `music.js`, `announce.js`, `planning.js`, `pwa.js`, `app/ui/router.py`. L'app n'a pas été lancée (login Entra + HA + Music Assistant requis) : les constats de rendu sont déduits du CSS, à valider par captures en début de lot 0.

---

## 1. Inventaire actuel

| URL | Rôle | Contenu |
|---|---|---|
| `/login` | public | une carte, bouton Entra |
| `/` | staff+ | 3 tuiles (Musique, Annonces, Admin). **Pas de tuile Alertes.** |
| `/music` | staff+ | Scènes, Lecteurs (+barre de sélection multiple), File, Recherche, dialogue de lecture |
| `/announce` | staff+ | Formulaire (10 champs), dialogue de prévisualisation, Historique (8 filtres), dialogue détail |
| `/alerts` | staff+ | Switch exercice, grille de boutons, dialogue de confirmation, tableau « Derniers évènements » |
| `/alerts/{id}` | staff+ | Récap/dashboard d'incident (chronologie, diffusion, relances auto, réponses SSE) |
| `/admin` | admin | **8 sections empilées** : Utilisateurs, Enceintes (+MA détectés), Groupes, Scènes, Modèles d'alerte, Voix, Messages pré-enregistrés ; lien vers Planification |
| `/admin/planning` | admin | Planifications, Calendrier de fermeture, Journal |

Rôles : admin, staff, student (student : aucune page hors login/offline, pas d'alerte).

---

## 2. Constats

Gravité : **P0** = sécurité/contrainte projet violée, **P1** = gêne forte au quotidien, **P2** = polish/cohérence.

### 2.1 Alerte (contrainte « 2 gestes partout »)

- **P0 — Le parcours réel fait 4 à 5 gestes**, pas 2 : (1) FAB « Alerte » → (2) page `/alerts` + scroll éventuel (le FAB ancre `#alert-grid` mais le switch exercice et le titre sont au-dessus) → (3) bouton de type → (4) **taper le mot « ALERTE »** → (5) « Diffuser ». Sur mobile, taper un mot au clavier en situation de stress est le pire point du parcours. À arbitrer (cf. §8, décision D1) : remplacer la saisie par un geste physique non accidentel (appui long 1,2 s / glisser pour confirmer) tout en gardant le mot pour le clavier.
- **P0 — L'alerte n'est atteignable que si le JS + la page `/alerts` chargent.** Hors ligne ou serveur lent : page offline sans alerte, et le bandeau hors-ligne dit « prévenir par sirène ou téléphone » mais ne donne **aucun numéro**. Il faut un numéro/consigne de secours configurable en dur dans la page offline.
- **P0 — Pas d'état global « incident en cours ».** Les scènes sont « bloquées pendant un incident » (texte en petit), les planifications ne tournent pas, mais aucune bannière globale ne le dit sur Musique/Annonces/Planification. L'utilisateur voit un bouton qui échoue sans comprendre. Il faut une bannière persistante (rouge, liée au récap) dans `base.html` tant qu'un incident est `sent`.
- **P1 — Le FAB disparaît sur `/alerts`** (normal) mais **n'existe pas pour d'autres écrans** : dialogues ouverts (top-layer natif `<dialog>` couvre le FAB), page login/erreur/offline. Un `<dialog>` modal (lecture Musique, prévisualisation Annonce) masque l'accès alerte.
- **P1 — Le mode exercice est un switch en haut de page, discret** : en `mode-real` (défaut) tout est rouge, en exercice la grille devient pâle+pointillée (bon). Mais le switch est une carte neutre : risque d'oubli du mode actif après scroll. Le bandeau TEST est caché hors exercice et non sticky. Proposer un badge de mode fixe dans la barre d'app (RÉEL/EXERCICE) lisible depuis n'importe où dans /alerts.
- **P1 — Le libellé du dialogue affiche toujours le mot réel** (`ad-confirm-label` utilise `alertWord` même en exercice ; le champ est masqué en test, donc sans conséquence visible, mais le mot de confirmation du mode test n'est jamais montré — incohérence latente).
- **P1 — Le mot de confirmation est servi en `data-alert-word` dans le HTML** : acceptable (c'est un garde-fou anti-erreur, pas un secret), mais à documenter comme tel ; ne pas le présenter comme sécurité.
- **P1 — Retour d'état de l'envoi** : toast 4 s (« Alerte diffusée ») puis fermeture du dialogue ; l'état de diffusion (enceintes ok/échec, Teams ok/échec) n'est pas montré avant d'aller dans le récap. Pour un outil de sécurité : après envoi, rediriger vers `/alerts/{id}` ou afficher un écran de résultat persistant, jamais un toast qui s'efface.
- **P1 — Échec d'envoi** : le toast d'erreur disparaît en 4 s et le dialogue se ferme même en erreur (`dlg.close()` après le try). L'utilisateur peut croire à un envoi. Garder le dialogue ouvert avec message d'erreur persistant + bouton « Réessayer ».
- **P2 — Récap `/alerts/{id}`** : statut brut anglais (`sent`, `closed`, `failed`), « ok / ignoré » en texte coloré seul (couleur seule = non accessible), 4 actions en ligne (CSV, PDF, relance…) qui passent à la ligne sur mobile, formulaire des relances auto exposé en permanence (intervalle en secondes, « vide = défaut, 0 = off » → charge cognitive). Le compteur « En danger » devrait être l'élément le plus gros de la page.
- **P2 — Tableau d'incidents** : colonne « Type » (code technique brut) en plus de « Titre », 8 colonnes, rafraîchissement doublé (`data-poll` + `setInterval` dans `app.js` : à unifier).

### 2.2 Navigation et hiérarchie

- **P0 — Accueil sans Alertes** (3 tuiles : Musique/Annonces/Admin). Le parcours le plus critique est le seul absent de l'accueil ; l'accès passe uniquement par le FAB ou le menu.
- **P1 — Accueil sans valeur** : pas d'état (lecteurs en cours, dernier incident, prochaine planification, Music Assistant/HA joignables). Il ne fait que dupliquer le menu.
- **P1 — Admin = page fleuve de 8 cartes**, 7 requêtes HTMX au chargement, aucun sommaire ni ancre ; trouver « Modèles d'alerte » demande un long scroll. Planification est un lien isolé en haut de l'admin (invisible du menu), alors que c'est une fonction métier (« Planification » dans la liste du brief) et pas de l'administration pure.
- **P1 — Navigation mobile** : 5 entrées + FAB + toast dans la même zone basse. Le toast (`bottom: nav-h + 16`) et le FAB (`bottom: nav-h + 16`, à droite) se **chevauchent** ; le FAB recouvre le dernier élément des listes/boutons à droite (boutons de suppression, pagination).
- **P1 — Pas de fil d'Ariane / retour** cohérent : `/alerts/{id}` a un lien retour, `/admin/planning` en a un, mais l'actif du menu reste « Admin » pour la planification.
- **P2 — Le nom d'utilisateur/e-mail n'apparaît que dans l'accueil** ; la barre n'affiche que le rôle (desktop) ; pas de menu compte. Bouton logout = icône seule sans confirmation ni texte.
- **P2 — Raccourcis clavier** : aucun (ex. `g a` → alertes). Utile en poste fixe d'accueil.

### 2.3 Densité

- **P1 — Annonces** : formulaire de 10+ champs au même niveau (preset, texte, groupes, enceintes, voix, langue, test, forcer volume…) alors que 90 % des usages = « texte + où ». Voix/langue/volume/test sont des options avancées.
- **P1 — Historique d'annonces** : 8 filtres affichés en permanence (2 lignes) avant le moindre résultat, plus pagination ; sur mobile cela repousse la liste hors écran.
- **P1 — Admin Enceintes** : chaque ligne est un formulaire complet de 5 champs (dont `notify_service`, `ma_player_id` en monospace) + bouton Enregistrer par ligne ; avec 20 enceintes = mur de champs. Pas de recherche, pas de vue tableau, pas d'état de santé (en ligne/hors ligne).
- **P1 — Scènes admin** : le macro `fields` = 13 champs + chips par scène, en `<details>` (bien) mais sans aperçu de ce que fait la scène.
- **P2 — Musique** : 4 cartes de même poids ; scènes (usage fréquent) en 1re position, bien, mais Lecteurs contient la barre de sélection masquée + liste + file dans une 3e carte séparée avec son propre sélecteur de lecteur (redondant avec la sélection cochée).

### 2.4 Cohérence

- **P1 — `.btn-danger` est défini deux fois** : la 1re (`color: var(--error)`, texte rouge) puis la 2e (`background: var(--error); color: var(--bg)`, remplie). Résultat réel : **tous les boutons de suppression, « Vider », etc. sont des pastilles rouges pleines**, au même poids visuel que « Diffuser l'alerte ». Détruit la hiérarchie : seule l'alerte réelle devrait être rouge plein.
- **P1 — Toggle actif/inactif** réimplémenté 6 fois (users, speakers, groups, scenes, closures, schedules) avec un bouton dont le libellé (« Active ») décrit l'état et non l'action ; icône toggle_on + couleur tonal/text = état par couleur ; pas d'`aria-pressed`/`role=switch`.
- **P1 — Statuts bruts techniques** affichés tels quels : `queued`, `sending`, `done`, `failed`, `skipped_closure`, `sent`, `draft`, `missed`. Aucun libellé français ni icône.
- **P1 — Les formulaires admin mélangent 3 patterns** : formulaire de création toujours ouvert (users, enceintes, groupes, presets), `<details>` repliable (scènes), formulaire d'édition inline par ligne (tout le reste). Pas de convention création vs édition.
- **P2 — Terminologie** : « Enceintes / lecteurs / players », « Groupe » (champ texte libre sur l'enceinte) **et** « Groupes d'enceintes » (entité) : deux notions homonymes dans l'écran Enceintes. « Messages pré-enregistrés » vs « Modèles d'alerte » vs « presets » côté code.
- **P2 — Heures** : UTC affiché pour « Prochaine » (`… UTC`) mais fuseau Europe/Paris saisi ; les dates `created_at[:16]` sont en UTC aussi. Afficher en Europe/Paris partout.
- **P2 — Charte** : docs/charte-graphique.md demande 24–32 px de padding et peu de bordures ; le FAB utilise une ombre dure (`box-shadow`) contraire à la charte (élévation tonale). Les `.btn-text` de 44 px de haut/16 px de padding OK.

### 2.5 Formulaires

- **P1 — Pas de validation visible** : aucun message d'erreur par champ, aucun `aria-invalid`, aucune `aria-describedby`. Les `pattern` natifs affichent les bulles navigateur non traduites/non cohérentes ; les erreurs serveur 422 sont agrégées en toast (`e.msg` concaténés) disparaissant en 4 s, sans lier l'erreur au champ.
- **P1 — Champs techniques sans aide** : `notify_service`, `ma_player_id`, `Entité HA`, `media_uri`, icône (« queue_music »), couleur (« #6750a4 ») sont des champs texte libres. Pour icône/couleur : sélecteur d'icône + color picker avec aperçu du bouton. Pour `ma_player_id` : le panneau « détectés » existe mais est séparé ; il faut un menu déroulant alimenté par les lecteurs détectés dans le formulaire même.
- **P1 — Envoi sans état de chargement** : seul `.htmx-request { opacity:.6 }` ; pas de désactivation du bouton → double envoi possible (annonce, création). L'annonce passe par `hx-confirm` natif `confirm()` : non stylé, non traduit sur certains navigateurs, bloquant en PWA iOS.
- **P1 — Confirmations destructives** : `hx-confirm` natif partout (suppression d'enceinte, de scène, d'utilisateur, « Vider la file », « Clore l'incident »). Aucun « annuler » après coup. Remplacer par un dialogue commun + toast avec « Annuler » pour les actions réversibles.
- **P2 — Select pour des booléens** (« Jours fermés », « Mode test » en `<select>` Oui/Non, « État » en select Actif/Inactif) : switches/cases attendus.
- **P2 — Labels** : `field` utilise `<span>` dans `<label>` (correct), mais les `fieldset.chips` n'ont pas de message d'erreur ni de compteur de sélection, et la liste de 20 enceintes en chips n'est pas filtrable.

### 2.6 Retours d'état

- **P1 — Toasts** : un seul `#toast` (écrase le précédent), 4 s, `role=status` aria-live polite pour **les erreurs aussi** (les erreurs devraient être `role=alert`), pas d'action « Annuler/Voir », pas de pile. Erreur 401 non récupérable → redirection brutale sans message.
- **P1 — États de chargement** : fragments HTMX chargés `hx-trigger="load"` sans placeholder : zone vide puis saut de mise en page. `hx-indicator="#queue"` pointe sur le conteneur lui-même (opacité) ; pas de squelette.
- **P1 — États vides** : textes `muted` sans action (« Aucune enceinte. » : sans lien « Ajouter »), sauf quelques-uns. « Cochez un lecteur pour voir sa file » ne dit pas où cocher (la case est dans une autre carte).
- **P1 — Rafraîchissements qui détruisent l'état** : `#players` se rafraîchit toutes les 8 s en `innerHTML` (sauf si un slider est focus) : perd le focus clavier d'un bouton lecture, les cases sont restaurées par JS mais un tap en cours peut tomber à côté. `#history` toutes les 10 s idem. Utiliser `hx-swap="morph"` (extension idiomorph) ou ne mettre à jour que les badges d'état.
- **P2 — Retour sur actions Musique** : lecture/pause/suivant n'ont pas d'état « en cours », l'icône ne change qu'après le refresh (jusqu'à 800 ms+). Mise à jour optimiste.

### 2.7 Mobile / PWA

- **P1 — Cibles tactiles** : `.btn` 44 px (min WCAG 24, cible Material 48) ; `.btn-icon` 44 px ; `.check` 44 ; chips 44 → globalement correct. **Mais** boutons d'icône côte à côte (queue-actions : monter/descendre/supprimer, 3×44 sans espace) : risque d'erreur de doigt sur « supprimer ». Les icônes `delete` pleines rouges (cf. 2.4) à côté de « toggle » = erreur probable.
- **P1 — Zone basse saturée** : nav 72 px + FAB 56 px + toast. Sur petits écrans (320×568) le contenu utile restant est faible, d'autant que le `appbar` sticky haut (≈ 64 px) s'ajoute.
- **P1 — Polices et icônes chargées depuis Google Fonts** (base.html) : (a) hors ligne ou réseau campus filtré, **les icônes Material Symbols s'affichent comme du texte (« emergency_home »)**, y compris sur le FAB et la nav ; (b) dépendance externe/RGPD. À héberger en local (`/static/fonts`) et à précacher dans le service worker.
- **P2 — Tableaux** : la bascule en cartes < 720 px (`data-label`) est bien faite pour incidents, **pas pour l'historique d'annonces** (`history.html` n'a pas de `data-label` → tableau avec défilement horizontal, `thead` masqué non, mais cellules écrasées).
- **P2 — Dialogues** : `<dialog>` natif modal (bon pour focus/clavier) mais `form method=dialog` + `Échap` ferme sans confirmation ; largeur `min(92vw,360px)`, pas de feuille du bas (bottom sheet) mobile, bouton principal pas ancré en bas.
- **P2 — Thème** : suit `prefers-color-scheme` sans bascule manuelle ; PWA OK pour theme-color.

### 2.8 Accessibilité

- **P1 — Icônes non masquées** : `<span class="icon">` sans `aria-hidden="true"` dans presque tous les templates (seuls base.html offline-banner et FAB le font) : les lecteurs d'écran lisent « music_note », « graphic_eq »…
- **P1 — Boutons icône sans nom accessible** : `title` seul (non fiable) sur précédent/suivant/supprimer/monter ; seul `history.html` ajoute `aria-label`. Logout = icône seule avec `title`.
- **P1 — Contraste** : `.muted` = `#4a5a63` sur `#fff`/surface-1 (≈ 7:1, ok) mais `--on-surface-variant` sombre `#a9b8c3` OK ; **à vérifier** : `.status-done` (`--ok #2e6b4a` sur surface-2, ≈ 5:1), `.badge` sur `surface-3`, bouton `.btn-danger` (`--error #b3261e` fond, texte `--bg #f8fafc` ≈ 6:1 clair ; en sombre `--error #f2b8b5` avec `--bg #0f172a` ok). Chips non sélectionnées : bordure `--outline #c3ccd1` sur `#fff` = **≈ 1,6:1, sous le 3:1 requis** pour un composant interactif. Placeholders non stylés.
- **P1 — Couleur seule comme information** : statut des incidents (couleur du texte), actif/inactif (tonal vs text), succès/échec de diffusion (vert/rouge sans icône), réponses « danger » (bordure rouge uniquement).
- **P1 — Switch du mode exercice** : bon (`role=switch`, focus ring), mais le changement de mode n'est pas annoncé (le `#mode-state` n'est pas dans une région live).
- **P1 — Dialogues** : `aria-labelledby` OK pour alert/preview/detail ; **`#play-menu` sans label** ; après fermeture le focus n'est pas rendu à l'élément déclencheur (le natif le fait pour `showModal`, à vérifier avec `data-opt`).
- **P2 — Navigation** : `aria-current` OK ; pas de lien d'évitement (« Aller au contenu ») ; `<main>` sans `id`.
- **P2 — `prefers-reduced-motion`** : géré (transitions) ; le `scroll-margin` du FAB OK.
- **P2 — `hx-trigger every`** : mise à jour de listes sans `aria-live` ni indication ; l'historique et la file se rechargent silencieusement.
- **P2 — Focus** : `outline` 2 px primary OK ; `.media` utilise `all: unset` puis restaure le focus (ok).

### 2.9 Divers techniques à garder en tête

- Pas de build : la solution doit rester CSS/JS vanille + HTMX. Idiomorph (≈ 4 ko) peut être vendu comme `htmx.min.js` l'est déjà.
- Les fragments `/ui/*` sont des routes de rendu séparées de l'API : la refonte peut créer de nouvelles routes UI sans toucher l'API métier (`/incidents`, `/announcements`…), ce qui limite le risque.
- `dashboard.html` = page nommée « dashboard » mais c'est un récap d'incident ; le dashboard « temps réel » global n'existe pas encore (home vide).

---

## 3. Principes de la refonte

1. **L'alerte d'abord** : toujours visible, toujours à 2 gestes, jamais derrière une navigation lourde, fonctionnelle hors ligne (page cachée par le SW avec numéros de secours).
2. **Un seul rouge plein** : le rouge plein = alerte réelle uniquement. Les suppressions sont rouges en texte/contour + confirmation.
3. **Orienté tâche** : accueil = état + 4 actions fréquentes ; l'admin = configuration, découpée.
4. **Progressive disclosure** : options avancées repliées, filtres repliés, formulaires d'édition dans panneau/dialogue.
5. **Une seule grammaire de composants** (cartes, listes de lignes, toggles, dialogues, toasts, états vides, chargements) pour réduire le CSS et les écarts.
6. **Pas de nouvelle dépendance de build** ; polices/icônes locales.

---

## 4. Architecture de navigation

### 4.1 Menu principal (staff)

Barre basse mobile = 4 entrées + bouton Alerte **intégré au centre** (plus de FAB flottant qui chevauche) ; desktop = barre latérale ou barre haute.

```
MOBILE (bas)                                  DESKTOP (haut ou rail gauche)
┌──────────────────────────────────┐          Sound   Accueil  Musique  Annonces  Planning  Admin ▾     [ALERTE]  👤
│ Accueil  Musique [ ALERTE ] Annonces  Plus │
└──────────────────────────────────┘          Le bouton ALERTE est rouge plein, à droite, toujours visible.
                                               « Plus » (mobile) = Planification, Admin, compte, thème, notifications, déconnexion.
```

Règles :
- **ALERTE** = 1er geste (tap) → ouvre **directement une feuille modale « Lancer une alerte »** sur n'importe quelle page (sans navigation), contenant la grille de types. 2e geste = choisir le type, qui mène à l'étape de confirmation (cf. D1). Le contenu de la feuille est précacheable (fragment `/ui/alerts/sheet`), avec repli : si le fragment échoue, naviguer vers `/alerts`.
- Dans les dialogues modaux (lecture Musique, preview Annonces), ajouter dans le dialogue un lien discret « Alerte » (le top-layer couvre le bouton global). Alternative : ne pas utiliser `<dialog>.showModal()` pour ces cas mais un panneau `popover`/non-modal qui laisse la barre accessible.
- **Planification** sort d'Admin et devient une entrée de menu (staff en lecture, admin en édition ? cf. D4). Si réservé admin, entrée visible admin seul.
- **Admin** : entrée avec sous-navigation (voir 4.2).

### 4.2 Admin éclaté

Mise en page : liste à gauche (desktop) / liste de rubriques puis détail (mobile, pattern master-detail). URL par rubrique pour pouvoir lier/mettre en favori.

```
/admin                      → tableau récap + santé (HA, Music Assistant, Teams, worker)
/admin/utilisateurs         → Utilisateurs et rôles
/admin/enceintes            → Enceintes (liste, santé) + onglet « Groupes »
/admin/enceintes/groupes
/admin/musique              → Scènes
/admin/annonces             → Messages pré-enregistrés, Voix par défaut
/admin/alertes              → Modèles d'alerte (+ paramètres : mot de confirmation, relances par défaut, numéros de secours)
/admin/planning             → (si conservé sous admin) Planifications / Fermetures / Journal en onglets
```

```
┌─ Admin ───────────────────────────────────────────────────────────┐
│ Utilisateurs │  Enceintes (24)     [ Rechercher… ]   [+ Enceinte]  │
│ Enceintes ◀  │  ─────────────────────────────────────────────────  │
│   Groupes    │  ● Salon 1      media_player.salon1   Groupe: Hall  ⋮│
│ Musique      │  ● Cantine      media_player.cantine  Groupe: Hall  ⋮│
│ Annonces     │  ○ Atelier  (hors ligne)                            ⋮│
│ Alertes      │  [Détecter les lecteurs Music Assistant]            │
│ Planning     │                                                     │
└──────────────┴─────────────────────────────────────────────────────┘
```

### 4.3 Raccourcis

- Clavier (desktop) : `g` puis `h/m/a/l/p` ; `/` focus recherche ; `Échap` ferme ; **`Maj+A`** ouvre la feuille d'alerte. Listés dans une aide `?`.
- Mobile PWA : `shortcuts` du manifest (Alerte, Annonce, Musique) pour l'appui long sur l'icône.
- Lien profond `/alerts?type=incendie` (ouvre directement la confirmation, utile pour un favori / QR code affiché).

---

## 5. Page d'accueil / tableau de bord

Objectif : en 5 secondes, savoir **si tout va bien**, et lancer l'action courante.

```
MOBILE
┌────────────────────────────────────┐
│ Bonjour Victor      ● Tout va bien │  ← pastille état (vert) ou bandeau rouge « INCIDENT EN COURS »
├────────────────────────────────────┤
│ [ 🔔 LANCER UNE ALERTE ]  (rouge)   │
├────────────────────────────────────┤
│ Actions rapides                    │
│ [📢 Annonce]  [🎵 Scène ▾]  [⏸ Tout]│
├────────────────────────────────────┤
│ En lecture (3/8)                   │
│ Cantine  ▶ Radio Nova      ──●─ 30 │
│ Hall     ⏸ —                       │
│                          Tout voir ›│
├────────────────────────────────────┤
│ À venir                            │
│ 12:00  Annonce « Repas »  (Hall)   │
│ 17:30  Scène « Soir »              │
├────────────────────────────────────┤
│ Dernières annonces / Dernier incident│
│ 11:02 Annonce — Hall — done        │
│ Dernier incident : exercice, 14/10 │
└────────────────────────────────────┘
```

Contenu :
- **État système** (admin et staff) : HA, Music Assistant, Teams, worker de planification ; chaque point cliquable → diagnostic (admin/…). Dégradations = bandeau ambre.
- **Incident en cours** : si `sent`, carte rouge en tête avec compteurs en direct (répondus / danger / sans réponse via SSE) et lien vers le récap.
- **Actions rapides** : Nouvelle annonce, 3 scènes favorites, Pause générale (déjà couverte par l'API groupée).
- **En lecture** : lecteurs actifs avec play/pause et volume.
- **Prochaines planifications** (3 prochaines), **dernières annonces** (5), **dernier incident**.
- Desktop : grille 12 colonnes (état + alerte en colonne droite fixe).

Dépendances : un endpoint `/ui/home/summary` agrégeant (nouveau fragment, pas de nouvelle logique métier lourde) ; SSE existant réutilisable pour l'incident.

---

## 6. Patterns communs (design system minimal)

À livrer dans `app.css` (section « composants ») + `app.js` (modules ; toujours sans build) + macros Jinja `templates/_ui.html`.

| Pattern | Spécification |
|---|---|
| **Carte** | `card` (surface-1, 24/32 px) ; variantes `card--flat` (listes), `card--alert` (bordure error). Une carte = une idée, titre h2 + action à droite (`card-head`). |
| **Liste de lignes** | `rowitem` : icône/avatar · titre + sous-titre · statut (badge) · action `⋮`. Remplace les formulaires inline par ligne. Édition = dialogue ou panneau latéral. |
| **Tableau** | `.table` + `data-label` obligatoire (macro), tri/filtres repliés (« Filtrer (2) »), pagination, densité compacte option. |
| **Dialogue** | un module `confirmDialog(title, body, {danger, confirmLabel})` remplace `hx-confirm` ; feuille du bas en mobile ; focus géré ; `Échap` ; bouton primaire désactivé pendant l'envoi. |
| **Toast** | pile (max 3), `role=status` pour succès, **`role=alert` + persistant jusqu'à fermeture pour erreur**, action optionnelle « Annuler » (suppressions réversibles par soft-delete côté serveur : `is_active=false` déjà présent, à étendre). Position haut sur mobile pour ne pas chevaucher la nav. |
| **Toggle** | composant unique `switch` (`role=switch`, `aria-checked`, libellé fixe « Active » + état par thumb, pas de changement de libellé). |
| **Badge de statut** | macro `status_badge(status)` : libellé FR + icône + couleur ; table de correspondance unique (queued=« En attente », sending=« En cours », done=« Envoyée », failed=« Échec »…). |
| **État vide** | macro `empty(icon, title, hint, cta)` ; toujours un CTA pour les rôles autorisés. |
| **Chargement** | squelettes (`.skeleton` CSS) dans les conteneurs `hx-trigger=load` ; `hx-indicator` ciblé ; boutons `aria-busy` + désactivés pendant la requête (utiliser `htmx:beforeRequest`). |
| **Erreurs de champ** | erreur serveur 422 mappée sur le champ (`aria-invalid`, `aria-describedby`, texte rouge + icône) ; résumé en haut du formulaire. |
| **Bouton** | hiérarchie : `primary` (1 par vue), `tonal`, `text`, `danger-text` (suppression), **`alert`** (réservé à l'alerte). Supprimer le doublon `.btn-danger`. |
| **Hiérarchie typo/espace** | jetons d'espacement (4/8/12/16/24/32), `page-head` standard (titre + actions + sous-titre d'état). |
| **Icônes** | Material Symbols locales + `aria-hidden` par défaut via macro `{{ icon('delete') }}` ; bouton icône = macro avec `aria-label` obligatoire. |
| **Accessibilité** | lien d'évitement, `<main id>`, bordures de contrôles ≥ 3:1 (`--outline` plus foncé), statut jamais par la couleur seule, `aria-live` sur zones auto-rafraîchies. |

---

## 7. Parcours clés (maquettes)

### 7.1 Lancer une annonce

Cible : 3 gestes pour l'usage courant (message prédéfini → lieu → envoyer), prévisualisation intégrée, options avancées repliées.

```
┌ Nouvelle annonce ─────────────────────────────┐
│ Message   (•) Préenregistré  ( ) Libre        │
│ ┌───────────────────────────────────────────┐ │
│ │ Fin de cours dans 5 minutes            ▾ │ │
│ └───────────────────────────────────────────┘ │
│ « Mesdames et Messieurs, le cours… »   (aperçu)│
│                                               │
│ Où diffuser ?                                 │
│ [Hall ✓] [Cantine] [Amphis] [Tout le campus]  │
│ ▸ Choisir des enceintes précises (12)         │
│   → 2 groupes · 5 enceintes ciblées           │
│                                               │
│ ▸ Options (voix, langue, volume, test)        │
│                                               │
│ [ Écouter l'aperçu ]      [ Envoyer ▶ ]       │
└───────────────────────────────────────────────┘
Après envoi : bandeau « Envoyée à 5 enceintes » → suit l'état (en cours → envoyée) en direct, lien « Voir dans l'historique ».
```

Changements :
- Mode « Libre » : textarea + compteur 500 ; sélecteur Préenregistré/Libre en bascule (au lieu de désactiver le textarea à la volée).
- Cibles : groupes en 1er (chips), enceintes en repli recherchable. Compteur persistant « N enceintes ». Bouton Envoyer désactivé tant que 0 cible, avec texte d'aide à côté (pas seulement grisé).
- `confirm()` natif remplacé par le dialogue d'aperçu existant, unifié : **un seul** écran de confirmation = aperçu (texte + cibles + avertissements) → Envoyer.
- Statut de l'envoi en direct (SSE ou poll court) plutôt qu'un toast ; échec → bouton « Réessayer ».
- Historique : filtre « Rechercher + Filtres (n) » repliable ; tableau en cartes sur mobile ; statut en français + icône ; action « Rediffuser » depuis une ligne.

### 7.2 Lancer une alerte

Cible : **2 gestes** depuis n'importe quelle page + garde-fou anti-erreur non clavier. (D1 à trancher.)

```
Geste 1 : bouton ALERTE (barre, toujours visible)
        ↓ feuille modale immédiate
┌ Lancer une alerte ─────────────────  Exercice [ off ] ┐
│ ┌────────────┐ ┌────────────┐ ┌────────────┐        │
│ │ 🔥 Incendie │ │ 🚨 Intrusion│ │ ⚠ Confinem.│        │
│ └────────────┘ └────────────┘ └────────────┘        │
│ ┌────────────┐ ┌────────────┐ ┌────────────┐        │
│ │ 🌪 Météo    │ │ ⚡ Panne    │ │ ✚ Malaise  │        │
│ └────────────┘ └────────────┘ └────────────┘        │
│ Mode : ALERTE RÉELLE (rouge) / EXERCICE (hachuré)    │
└───────────────────────────────────────────────────────┘
        ↓ Geste 2 : toucher le type
┌ INCENDIE — ALERTE RÉELLE ─────────────────────────────┐
│ « Évacuation immédiate … »  (message exact)           │
│ Sera diffusé sur : 24 enceintes + Teams              │
│                                                       │
│ [ Annuler ]   [ ═══ Maintenir 1,2 s pour diffuser ═══ ]│  ← option A (appui long, 1 geste, tactile+clavier : Entrée maintenue)
│        ou   tapez ALERTE pour confirmer  (option B)   │  ← clavier, saisie conservée si D1 = garder le mot
└───────────────────────────────────────────────────────┘
        ↓ après envoi (écran persistant, pas de toast)
┌ ALERTE ENVOYÉE 14:02:11 ─────────────────────────────┐
│ Enceintes ✓ 24/24   Teams ✓   Réponses : 12 · 0 danger│
│ [ Ouvrir le récap ]   [ Clore ]                       │
└───────────────────────────────────────────────────────┘
En erreur : l'écran reste, détaille quoi a échoué, « Réessayer » + consigne de secours (téléphone/sirène).
```

Règles : mode exercice explicite dans la feuille (switch en tête, état = bordure pointillée + badge « EXERCICE » dans la barre, non persistant) ; la validation ne bloque **jamais** l'envoi si le JS de confirmation est en défaut (repli : bouton simple à double appui) ; fonctionne hors dialogue Musique/Annonces ; jamais bloqué par un incident déjà ouvert (nouvelle alerte autorisée, avec avertissement « Un incident est déjà en cours »).

Récap `/alerts/{id}` : bandeau fixe de compteurs (DANGER en premier et plus grand), tri des répondants par danger puis récents, actions principales en barre sticky (Relancer / Clore), exports et paramètres des relances repliés dans « Plus ».

### 7.3 Régler la musique

Cible : lecteurs + scènes + volume en un écran ; la file en tiroir ; recherche en haut.

```
┌ Musique ───────────────────────── [🔎 Rechercher…] ┐
│ Scènes   [Matin] [Midi] [Soir] [Silence]  (désact. si incident, avec explication) │
│                                                     │
│ Lecteurs        ( ) Tous  [Groupe Hall] [Cantine]   │
│ ┌─────────────────────────────────────────────────┐ │
│ │ ☑ Cantine   ▶ Radio Nova           ⏮ ⏸ ⏭       │ │
│ │              ───────●───── 30%      [File ▸]    │ │
│ ├─────────────────────────────────────────────────┤ │
│ │ ☑ Hall      ⏸ —                    ⏮ ▶ ⏭       │ │
│ └─────────────────────────────────────────────────┘ │
│ ─ Barre de sélection collante (2) : ⏮ ⏯ ⏭  🔊──●── │ │
│ ▸ File d'attente — Cantine (12)  (tiroir à droite / bottom sheet) │
└─────────────────────────────────────────────────────┘
```

Changements : barre de sélection collante en bas de zone (pas dans la carte) ; file en tiroir liée au lecteur ciblé (supprime le sélecteur redondant) ; mise à jour optimiste des boutons ; rafraîchissement sans perte de focus (morph) ; scènes désactivées visuellement avec raison pendant un incident ; recherche dans une feuille plein écran avec choix « Lire maintenant / ensuite / ajouter » intégré, cible affichée (« sur : Cantine, Hall »).

### 7.4 Configurer une enceinte

Cible : onboarding guidé au lieu de 5 champs libres.

```
Admin > Enceintes > [+ Enceinte]
┌ Nouvelle enceinte — étape 1/3 ───────────────┐
│ 1 Détecter    2 Nommer      3 Tester          │
│ Lecteurs trouvés dans Music Assistant :       │
│  (•) Cantine (id: …a4f2)   libre              │
│  ( ) Hall    (id: …91c0)   déjà utilisée par Hall │
│ Entité Home Assistant  [ media_player.cantine ▾ ] (liste HA)│
│ Service de notification [ notify.alexa_… ▾ ] (optionnel)  │
│              [ Suivant ]                       │
├ étape 2 : Nom [Cantine]  Groupes [Hall ✓]      │
├ étape 3 : [ 🔊 Jouer un son de test ]  → ✓ Entendu / ✗ Problème │
└───────────────────────────────────────────────┘
Fiche enceinte : état (● en ligne), dernière diffusion, bouton Tester, champs techniques repliés « Avancé », Désactiver / Supprimer (avec dialogue).
```

Changements : champs `entity_id`/`ma_player_id`/`notify_service` en listes alimentées (HA et MA), validation en direct (format + existence), détection de doublons, bouton « Tester l'enceinte ». Le champ texte libre « Groupe » de l'enceinte disparaît au profit des groupes d'enceintes (une seule notion).

---

## 8. Décisions à trancher par l'utilisateur

- **D1 — Confirmation d'alerte réelle** : garder la saisie du mot « ALERTE » (4 gestes + clavier) ; ou appui long de 1,2 s en tactile **en conservant le mot en option clavier** ; ou double confirmation par 2 taps. Impact : respect de « 2 gestes » vs garde-fou anti-fausse manip. Recommandation : appui long (tactile) + mot (clavier) ; exercice = 1 tap.
- **D2 — Forme de la navigation** : barre basse (mobile) + rail latéral (desktop) avec bouton Alerte central/permanent, ou garder barre haute + FAB corrigé. Recommandation : bouton Alerte intégré à la barre, FAB supprimé.
- **D3 — Accueil** : tableau de bord complet (état + lecteurs + planning) ou simple (alerte + 4 raccourcis + état incident) pour un lot 1 plus léger ?
- **D4 — Planification** : en menu principal pour staff (lecture seule) ou réservée admin sous Admin ? Éditer reste admin ?
- **D5 — Hébergement des polices/icônes en local** (Inter/Roboto/JetBrains Mono/Material Symbols) : accord pour embarquer les fichiers (~1 Mo) et les précacher ?
- **D6 — Suppressions** : soft-delete + « Annuler » (toast 8 s) pour les entités réversibles, ou simple dialogue de confirmation ? (le soft-delete demande un petit changement serveur pour users/scènes/enceintes.)
- **D7 — Fuseau d'affichage** : tout en Europe/Paris (au lieu d'UTC) — confirmer.
- **D8 — Raccourcis clavier** et `shortcuts` PWA : souhaités ou hors périmètre ?
- **D9 — Rôle `student`** : aucune page ? Une page publique « Alerte en cours » en lecture seule est-elle voulue ?
- **D10 — Numéros de secours** à afficher hors ligne : où les configurer (variable d'environnement/paramètre admin, jamais en dur) et lesquels.

---

## 9. Découpage en lots livrables et ordre

Principe : chaque lot est indépendant, livrable, et laisse l'alerte plus accessible que la veille. Les lots 0 à 2 ne changent aucune API.

### Lot 0 — Fondations et corrections sans risque (≈ 1–2 j)
- Captures de référence de toutes les pages (mobile 375 px / desktop 1280 px, clair/sombre) via Playwright headless + jeu de données seed ; ces captures servent de base de non-régression visuelle.
- Corriger `.btn-danger` (doublon), `aria-hidden` sur toutes les icônes (macro `icon()`), `aria-label` des boutons icône, `role=alert` pour toasts d'erreur, contraste `--outline`, chevauchement toast/FAB, `#play-menu` labellisé, `data-label` sur l'historique.
- Auto-héberger polices + icônes (selon D5) et ajouter au précache du SW.
- Unifier le polling des incidents (retirer le `setInterval` doublon).

### Lot 1 — Alerte atteignable en 2 gestes (≈ 3–4 j) — prioritaire
- Bouton Alerte permanent dans la barre (D2), feuille modale d'alerte globale, confirmation selon D1, écran de résultat persistant, erreurs non fermantes, bandeau « incident en cours » global, badge de mode.
- Page offline avec consignes de secours (D10) ; lien profond `?type=`.
- Tests : E2E Playwright du parcours (hors ligne, serveur 500, 401 → refresh), test axe (a11y).

### Lot 2 — Design system minimal (≈ 3–4 j)
- Macros Jinja (`icon`, `status_badge`, `empty`, `switch`, `confirm`), toasts pile + persistant, `confirmDialog`, squelettes, boutons `aria-busy`, erreurs de champ.
- Remplacement progressif des `hx-confirm` et des toggles dupliqués.

### Lot 3 — Navigation et Admin éclaté (≈ 3–4 j)
- Sous-routes `/admin/*` (anciennes ancres redirigées), master-detail, planification dans le menu (D4), fil d'Ariane, actif correct du menu. Redirection `/admin` → récap/santé.

### Lot 4 — Accueil / tableau de bord (≈ 3 j)
- Fragment `/ui/home/summary` (état système, incident en cours, lecteurs actifs, planifs à venir, dernières annonces) ; tuile Alertes ajoutée **dès le lot 0 en correctif** si lot 4 tardif.

### Lot 5 — Annonces (≈ 3 j)
- Formulaire simplifié (modes préenregistré/libre, ciblage compact, options repliées), aperçu unifié, suivi de statut en direct, historique à filtres repliés + cartes mobile + libellés FR + « Rediffuser ».

### Lot 6 — Musique (≈ 3–4 j)
- Barre de sélection collante, file en tiroir, mise à jour optimiste, morph (idiomorph) pour polling sans perte de focus, recherche en feuille, scènes bloquées expliquées.

### Lot 7 — Config enceintes/scènes/modèles (≈ 4–5 j)
- Onboarding enceinte (listes HA/MA, test sonore), fiche enceinte, sélecteurs icône/couleur avec aperçu, scènes avec aperçu, modèles d'alerte avec aperçu du bouton et du message.

### Lot 8 — Récap d'incident et planification (≈ 2–3 j)
- Récap : compteurs DANGER en tête, barre d'actions sticky, paramètres repliés, statuts FR. Planification : formulaire en étapes/dialogue, vue calendrier de prochaines exécutions, fuseau Europe/Paris (D7).

Ordre : 0 → 1 → 2 → 3 → 4 → 5 → 6 → 8 → 7. Le lot 1 ne dépend pas du 2 (utilise du JS autonome) mais gagnera à être refait avec les macros du lot 2 si D1 touche au dialogue.

---

## 10. Risques de régression et parades

| Risque | Parade |
|---|---|
| Rendre l'alerte plus fragile (JS qui échoue, feuille non chargée) | Garder `/alerts` fonctionnel et indépendant du JS de feuille ; repli par navigation ; test E2E sur chaque PR ; ne pas toucher `POST /incidents/from-template` ni le contrat `confirm`. |
| Faux envoi d'alerte réelle (appui long accidentel, mode exercice oublié) | Mode exercice jamais persisté (comme aujourd'hui), badge de mode permanent, temporisation d'appui long, journal d'audit inchangé ; test de la matrice mode × confirmation. |
| Changer les `id` HTMX (`#players`, `#history`, `#incidents`…) casse `app.js`/`music.js`/`announce.js` | Garder les ids existants ; cartographier les `data-*` (`data-refresh`, `data-json`, `data-sse`, `data-reload`) avant d'extraire des macros ; tests de fumée par page. |
| Routes `/admin/*` : liens ou favoris cassés, ancres | Redirections 301 de `/admin` et `/admin/planning` ; test d'accès par rôle (staff ≠ admin, student). |
| Service worker : cache d'anciens CSS/JS après refonte | Versionner le SW (`sw.template.js`), invalider le cache, tester la mise à jour PWA sur iOS/Android. |
| Polices/icônes locales manquantes → icônes en texte | Précache + `font-display: block` pour les icônes + repli textuel (aria-label). |
| Idiomorph / morph casse les états de sliders et focus | Introduire page par page ; conserver l'exclusion « slider actif ». |
| `dialog`/`hx-confirm` remplacés : actions destructives sans confirmation | Un seul `confirmDialog` testé ; interdiction de supprimer sans confirmation dans la revue. |
| Régression visuelle (dark/clair, 320 px, zoom 200 %) | Captures de non-régression du lot 0 ; revue sur 320/375/768/1280 ; test axe. |
| Perte de fonction de sélection multi-lecteurs (localStorage `sound.music.selection`) | Conserver la clé et le format ; migration silencieuse. |
| Ouverture de nouvelles requêtes (accueil) ralentit le premier rendu | Fragments chargés en différé, squelettes, cache court côté serveur ; la page ne bloque jamais sur HA/MA indisponibles (états dégradés). |
| Traduction/libellés de statut : divergence avec l'API/CSV/PDF | Libellés uniquement côté UI ; exports et API inchangés. |

---

## 11. Critères de réussite

- Alerte : ≤ 2 gestes depuis toute page (hors dialogue modal de l'alerte elle-même), mesuré par test E2E ; zéro toast comme seule confirmation d'une alerte.
- Aucune page > 3 écrans mobiles de hauteur sans navigation interne (Admin éclaté).
- 0 violation axe « serious/critical » ; contrastes ≥ 4,5:1 (texte) / 3:1 (composants).
- Tous les boutons icône ont un nom accessible ; toutes les icônes décoratives `aria-hidden`.
- Un seul style de bouton destructif ; un seul composant toggle ; un seul mécanisme de confirmation.
- Aucun secret/IP/numéro en dur dans les templates ou le JS (numéros de secours via configuration).

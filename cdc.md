Cahier des Charges : Application Web de Gestion des Alertes et Diffusion (Campus Epitech Moulins)
1. Contexte et Objectifs
Le projet consiste à développer une application Web responsive (compatible mobile et desktop) pour l'infrastructure de campus d'Epitech Moulins. L'application permet de piloter la diffusion audio via Music Assistant et de déclencher des scripts d'annonces vocales ou des alertes de sécurité synchronisées avec Home Assistant, tout en assurant une communication d'urgence ciblée sur Microsoft Teams.
2. Architecture Technique
Back-end : Python / FastAPI (asynchrone, haute performance, documentation automatique via Swagger/OpenAPI).
Front-end : Interface Web Responsive (Tailwind CSS / HTML dynamique, pensée "Mobile-First" pour un usage tactile rapide sur smartphone).
Intégrations API :
Music Assistant API (gestion des flux musicaux, lecture, file d'attente, zones de diffusion).
Home Assistant API (déclenchement de scripts d'annonces et synthèse vocale TTS sur les enceintes Amazon Echo du campus).
Microsoft Graph API / Teams (envoi d'alertes urgentes et de cartes adaptatives interactives).
3. Gestion des Utilisateurs et Hiérarchie des Rôles
L'accès à l'application est sécurisé par authentification par jeton (JWT / Bearer Token). Les rôles définissent les permissions et les niveaux de priorité dans les files d'attente :

Administrateur : Accès total (gestion des utilisateurs, configuration des API, diffusion prioritaire absolue, supervision globale).
Équipe Pédagogique / Staff : Droits de diffusion de messages, déclenchement des alertes et gestion musicale de niveau équivalent.
Invité / Étudiant (le cas échéant) : Accès restreint selon les besoins opérationnels.
4. Fonctionnalités Détaillées
4.1. Module Musical (Music Assistant)
Recherche dans les bibliothèques et services de streaming associés.
Sélection des lecteurs actifs (zones d'enceintes).
Contrôles de lecture de base : Play, Pause, Suivant, Précédent, Réglage du volume.
4.2. Module de Messages et Annonces Vocales (Home Assistant)
Saisie de texte libre pour génération de synthèse vocale (TTS).
Menu déroulant de messages pré-enregistrés pour un déclenchement rapide.
Option de marquage explicite "Ceci est un test" lors de l'envoi pour éviter toute confusion en situation réelle.
4.3. Gestion des Alertes de Sécurité & Urgences (Teams & Alexa)
Déclenchement simultané : Envoi d'un message d'alerte vocal prioritaire sur les enceintes connectées du campus et diffusion d'une Carte Adaptative interactive sur un canal Teams dédié.
Cartes Adaptatives Teams (Interaction Étudiants) :
Les étudiants reçoivent une notification urgente persistante jusqu'à lecture.
Options de réponse intégrées :
« En sécurité, sur le campus »
« En sécurité, hors campus »
« En danger / Besoin d'aide » (avec un champ de texte libre optionnel pour préciser la situation exacte).
Tableau de Bord Administrateur (Supervision en temps réel) :
Suivi instantané des retours étudiants.
Listes distinctes des répondants (avec leur statut et localisation/précisions) et des non-répondants.
5. Sécurité et Contraintes
Aucun identifiant de services tiers (comptes de streaming) ne transite ou n'est stocké dans l'application tierce ; la gestion des comptes s'appuie entièrement sur les passerelles configurées dans Music Assistant.
Gestion des files d'attente prioritaires dans FastAPI : les alertes de sécurité et les requêtes administrateur court-circuitent les flux standards.

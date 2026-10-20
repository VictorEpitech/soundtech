# Design System & Charte Graphique - V-Tech

## 1. Philosophie et Identité
Ce document définit les directives UI/UX pour la génération de l'application. L'objectif est de s'éloigner d'un rendu "IA générique" pour adopter une identité forte, ancrée dans le **Material Design 3 (MD3)**. 
Les maîtres-mots de l'interface sont : **Expertise technique, Sécurité, Confiance, Minimalisme et Avenir**.

## 2. Palettes de Couleurs (Thème Dynamique MD3)
L'application doit supporter nativement le mode clair et le mode sombre. Pour éviter les clichés du "bleu tech standard" (trop classique) ou du "vert terminal" (trop vu), nous optons pour des teintes sophistiquées qui inspirent la solidité d'une architecture bien conçue.

### Mode Clair (Institutionnel & Confiance)
*   **Primary (Action principale) :** Bleu Ardoise profond (`#2A4B5A`) - Rassurant, stable, professionnel.
*   **On-Primary (Texte sur Primary) :** Blanc (`#FFFFFF`)
*   **Secondary (Éléments secondaires) :** Gris Sauge (`#5A717B`)
*   **Tertiary (Accents/Avenir) :** Améthyste doux (`#7D5260`) - Apporte une touche d'innovation inattendue.
*   **Background :** Blanc cassé très léger (`#F8FAFC`) - Évite l'effet "éblouissant" des blancs purs.
*   **Surface :** Blanc pur (`#FFFFFF`)

### Mode Sombre (Outil Technique & Supervision)
*   **Primary :** Cyan-Glacier (`#8BD3E6`) - Lumineux, rappelant les indicateurs d'état réseau, mais non agressif.
*   **On-Primary :** Bleu très sombre (`#003544`)
*   **Secondary :** Bleu Acier désaturé (`#B1CBD6`)
*   **Tertiary :** Lavande (`#EFB8C8`)
*   **Background :** Gris Nuit profond (`#0F172A`) - Plus moderne et profond que le noir pur ou le gris neutre par défaut du MD3.
*   **Surface :** Gris Nuit surélevé (`#1E293B`)

## 3. Typographie
La hiérarchie typographique joue sur le contraste entre la réassurance institutionnelle, la lisibilité standard et l'ADN technique.

*   **Titres (Headings - H1 à H6) :** `Inter`. Une typographie sans-serif sobre, géométrique mais très lisible. Elle apporte le côté "confiance et structure".
*   **Corps de texte (Body) :** `Roboto`. Le standard du MD3, fluide et parfait pour la densité de lecture.
*   **Données Techniques & Badges (Monospace) :** `JetBrains Mono` (ou à défaut `Fira Code`). À utiliser **strictement** pour mettre en évidence l'expertise : adresses IP, états de terminaux, requêtes réseau, snippets de code, ou métriques système. Cela crée un pont visuel fort vers l'univers du développement et de l'administration système.

## 4. Formes et Élévation (Style MD3)
L'interface embrasse les guidelines organiques du Material Design 3 pour contrebalancer la rigueur technique par une UX accueillante.
*   **Angles (Corner Radius) :** Très arrondis. Utiliser les tokens MD3 standard (ex: 28px pour les grosses cartes, 16px pour les éléments moyens, "Pill shape" / arrondis complets pour les boutons d'action principaux et FAB).
*   **Élévation :** Pas d'ombres portées dures (Drop Shadows). L'élévation doit se faire via les teintes de surface (*Tonal Elevation*). En mode sombre, les surfaces plus hautes deviennent subtilement plus claires, simulant une lumière directionnelle.

## 5. Densité et Mise en page (Layout)
*   **Minimalisme :** L'interface doit "respirer". Utiliser des marges généreuses (padding de 24px à 32px sur les conteneurs principaux).
*   **Séparation :** Privilégier l'espace vide (whitespace) pour séparer les groupes d'informations. Éviter la multiplication des bordures ou des lignes séparatrices (Dividers), sauf pour des tableaux de données denses.

## 6. Iconographie
*   **Bibliothèque :** Material Symbols (Google).
*   **Style :** `Outlined` (Tracé fin).
*   **Propriétés recommandées :** Weight: 300, Grade: 0. 
*   **Rendu :** Les icônes doivent être élégantes, fines et modernes. Le contraste entre le tracé fin des icônes et les formes pleines/arrondies des boutons crée un équilibre visuel premium.

## 7. Mouvement et Micro-interactions
*   **Fluidité sans fioritures :** Les animations doivent servir la fonctionnalité et non la décoration pure.
*   **Durée :** Courtes et percutantes (200ms - 300ms).
*   **Easing :** Utiliser les courbes MD3 standards (`Emphasized decelerate` pour l'apparition des éléments, `Emphasized accelerate` pour les sorties). 
*   **Interactions :** Retour visuel immédiat (effet *Ripple* léger sur les clics, transitions de couleur douces sur les états hover/focus/active). Aucune animation rebondissante.

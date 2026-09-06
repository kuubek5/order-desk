# Ринок систем для фрезерного/зуботехнічного виробництва — порівняння з KuubMill

Дата дослідження: 05.09.26. Джерела — офіційні сайти вендорів, документація, огляди (Capterra/G2/SoftwareAdvice), блоги галузевих видань (LMTmag, Dental Products Report). Точність фіч — на рівні маркетингових матеріалів вендора (немає доступу до демо/тріалу для верифікації); там, де інформація неповна, позначено «~» (частково/не підтверджено).

## 1. Досліджені системи

### Категорія A — спеціалізовані dental lab management / milling-center

| # | Система | Ринок / тип | Джерело |
|---|---|---|---|
| A1 | **Evident (EviSmart)** | US, повноцінна LMS для лабораторій будь-якого розміру | [evidentdigital.com](https://www.evidentdigital.com/smart-lab/lab-management-solutions), [EviSmart CaseEntry](https://www.evidentdigital.com/blog/evident-launches-evi-smart-case-entry-to-eliminate-manual-case-entry-for-dental-labs) |
| A2 | **Labtrac** | US, одна з найстаріших LMS, on-prem+cloud портал | [labtrac.com/Features](https://www.labtrac.com/Features), [labtrac.online](https://www.labtrac.online/en/Home/Features) |
| A3 | **Magic Touch DLCPM (Signature/Enterprise)** | US, LMS + LabConneX (портал клініки) + мобільні app для техніків/кур'єрів | [magictouchsoftware.com](https://magictouchsoftware.com/dlcpm-enterprise/), [Lab Connex](https://magictouchsoftware.com/lab-connex/) |
| A4 | **LabStar** | US, cloud LMS «від кейса до інвойсу» | [LMTmag LabStar](https://lmtmag.com/labstar), [softwaresuggest.com/labstar](https://www.softwaresuggest.com/labstar) |
| A5 | **iLab (DentalLabPortal-клас)** | Cloud LMS, безкоштовний тариф для лабораторій <100 кейсів/міс | [ilab.dental](https://ilab.dental/) |
| A6 | **GreatLab (Great Lakes-клас Lab Manager)** | Cloud LMS, аналітика й трасування зразків | [greatlab.cloud](https://greatlab.cloud/lab-management-software/) |
| A7 | **Dental Lab Guru** (EU — Нідерланди/Німеччина) | Європейська LMS, заміна Excel-таблиць | [dentallabguru.com](https://www.dentallabguru.com/) |
| A8 | **3Shape Communicate / Dental Manager / LMS** | DK, інтеграція CAD (Dental System) з LMS-провайдерами (Labtrac, MagicTouch, LabStar, Protetiko, Jenmar) | [3shape.com/lms](https://www.3shape.com/en-us/software/lms), [3Shape LMS-integration req](https://support.3shape.com/lab-dental-system-setup-how-to/what-is-required-for-integrating-lab-management-system-with-3shape-dental-system) |
| A9 | **exocad dentalshare** | DE, файлообмін клініка↔лаба↔виробничий центр, без FTP | [exocad.com/dentalshare](https://exocad.com/our-products/dentalshare/) |
| A10 | **Zirkonzahn.Archiv / App / Fräsen** | IT, екосистема ПЗ для власного CAD/CAM+фрезерування | [zirkonzahn.com CAD/CAM Software](https://zirkonzahn.com/us/cad-cam-systems/cad-cam-software) |
| A11 | **Amann Girrbach Ceramill (Match2 / AG.Live / Matik)** | AT, «job management → machine queueing → production», RFID-облік матеріалу | [amanngirrbach.com/software](https://www.amanngirrbach.com/en-us/catalog/software/), [Ceramill Matik](https://www.amanngirrbach.com/en-us/equipment/production-cam/ceramill-matik/) |
| A12 | **Argen ArgenLink** | US, milling-center-specific — cloud-портал замовлення фрезерування/друку/SLM | [argen.com/link](https://argen.com/link), [LMTmag ArgenLink](https://lmtmag.com/articles/9-software-innovations-argen-digital-launches-argenlink) |

*(Cusp Dental Software виявився практикою-менеджментом для клінік, не milling-center — виключено з матриці; замінено на Argen ArgenLink, який точно milling-center-specific.)*

### Категорія B — виробничі MES/job-shop

| # | Система | Джерело |
|---|---|---|
| B1 | **Fulcrum** | [fulcrumpro.com](https://fulcrumpro.com/), [job tracking](https://fulcrumpro.com/manufacturing-software/job-tracking) |
| B2 | **Odoo Manufacturing (MRP)** | [odoo.com/app/manufacturing](https://www.odoo.com/app/manufacturing), [Odoo 19 docs](https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/manufacturing.html) |
| B3 | **ProShop ERP** | [proshoperp.com](https://proshoperp.com/product/), [traceability blog](https://proshoperp.com/blog/achieving-full-traceability-shop-floor/) |
| B4 | **JobBOSS²** (ECI) | [ecisolutions.com/jobboss2](https://www.ecisolutions.com/products/jobboss2/features/) |

### Категорія C — CRM/трекери (лише UI-патерни)

| # | Система | Джерело |
|---|---|---|
| C1 | **Linear** | [Linear Docs](https://linear.app/docs/assigning-issues), [925studios breakdown](https://www.925studios.co/blog/linear-design-breakdown-saas-ui-2026), [fastshortcuts.com](https://fastshortcuts.com/shortcuts/linear/) |
| C2 | **HubSpot (deal board)** | [HubSpot Knowledge — sales workspace](https://knowledge.hubspot.com/prospecting/create-and-manage-deals-in-the-sales-workspace), [INSIDEA board views](https://insidea.com/blog/hubspot/kb/how-to-customize-board-views-for-pipelines-in-hubspot/) |
| C3 | **Jira (board)** | [Atlassian swimlanes](https://support.atlassian.com/jira-software-cloud/docs/configure-swimlanes/), [Jira launch notes](https://jirareleases.atlassian.com/announcements/a-faster-more-flexible-board-for-software-teams-1) |

---

## 2. Матриця «функція × система»

Позначення: ✓ = є як заявлена фіча, ~ = частково/непряма підтримка або не підтверджено з відкритих джерел, — = немає/не знайдено.

### 2.1 Категорія A (dental lab / milling-center)

| Функція | Evident | Labtrac | MagicTouch | LabStar | iLab | GreatLab | DentalLabGuru | 3Shape LMS | exocad share | Zirkonzahn | Amann Girrbach | Argen Link | **KuubMill** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Унікальний ID кейса / трекінг | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (наряд+job_code+sum3d_id) |
| Стадії/статуси кейса | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ~ | ~ | ✓ | ✓ | ✓ (§5, з двох полів, не окремий статус) |
| Дедлайн/терміновість (rush) | ✓ (авто-нотифікації) | ✓ (dynamic rescheduling) | ~ | ~ | ~ | ~ | ~ | ~ | — | — | ✓ (черга завантаження) | ✓ (cut-off часи) | ~ (дедлайни свідомо ІГНОРУЮТЬСЯ, є лише текстовий сигнал «на швидку», §2) |
| Штрихкод/QR-мітки | ~ | ✓ (touchscreen/barcode) | ~ | ~ | — | — | — | — | — | — | ✓ (RFID на бланках) | — | — (немає спільного ключа рядок↔коронка, звірка оком+STL, §2 правило 3) |
| Партія/лоток (batch/tray) | ~ | ✓ (виробниче планування) | ~ | ~ | — | — | — | — | — | ✓ (sub-projects) | ✓ (RFID-бланки, черга верстата) | ✓ (batch upload) | ✓ (часткова видача — норма, «Видати N з M», §2/§9.4) |
| Спікання/пічка — планування | — | — | — | — | — | — | — | — | — | — | — | — | — (моніторинг статусу пічки, НЕ планування закладки) |
| Портал клієнта (self-service) | ✓ | ✓ (cloud-портал) | ✓ (LabConneX) | ✓ | ✓ | ~ | ✓ | ✓ (Dental Manager inbox) | ✓ (dentalshare) | ~ (Zirkonzahn Cloud) | ~ (AG.Live) | ✓ (ArgenLink) | — (Google Таблиця замість порталу, доступ читання/коментар) |
| Інвойсинг/білінг | ✓ | ✓ (paperless) | ✓ | ✓ | ✓ | ~ | ✓ | — | — | — | — | ~ | — (свідомо: «CRM гроші не рахує», §5) |
| Переробка/брак (remake) з трасуванням | ~ | ✓ («reworks» у лайфсайклі) | ~ | ~ | ~ | ~ | ~ | — | — | — | — | ✓ (reorders/cancellations) | ✓ (детально: винен обладнання/технік/адмін/клієнт + к-сть, §3/§5) |
| QC-чеклисти | ✓ (compliance) | ~ | ✓ (quality control module) | ~ | ~ | ✓ (quality checks) | ~ | — | — | — | — | — | — (лише категоризація браку, без чеклистів) |
| Сповіщення/SMS | ✓ | ~ | ✓ (Bench App нотифікації) | ~ | ~ | ✓ | ~ | ~ | — | — | — | ✓ (dashboard updates) | — (внутрішні, без SMS клієнтам) |
| Дашборди/аналітика | ✓ | ✓ (reports) | ✓ (BI) | ✓ | ~ | ✓ (KPI, turnaround) | ~ | ✓ (unified dashboard) | — | — | ~ | ✓ (real-time dashboard) | ✓ (§9.5 Статистика, §9.9 Виробіток) |
| Мобільний застосунок | ~ | ✓ (tablet) | ✓ (Bench App, Route Manager) | ~ | ~ | ~ | ~ | ~ | — | ✓ (App) | ✓ (10" touchscreen) | ~ | — (десктоп/браузер, немає нативного мобільного) |
| Скан/CAD-імпорт (3Shape/exocad/IOS) | ~ | ✓ (інтегровано з 3Shape) | ~ | ✓ | ~ | ✓ | ~ | ✓ (нативно) | ✓ (нативно) | ✓ (нативно) | ✓ (нативно) | ✓ (drag&drop з Dental Manager) | — (файли приймаються як є, без CAD-парсингу) |
| Етикетки доставки/логістика | ~ | ~ | ✓ (Route Manager) | ✓ (UPS/FedEx інтеграція) | ~ | — | ~ | — | — | — | — | ✓ (package tracking) | — (видача через оператора/кур'єра вручну, §2) |
| Звіти по техніках/виробітку | ✓ | ✓ | ✓ (payroll reports) | ✓ | ~ | ✓ | ~ | ~ | — | — | ~ | — | ✓ (§9.9 Виробіток — саме для ЗП) |

### 2.2 Категорія B (MES/job-shop) — для орієнтиру за виробничими патернами

| Функція | Fulcrum | Odoo Mfg | ProShop | JobBOSS² | **KuubMill** |
|---|---|---|---|---|---|
| Динамічне планування/переприоритезація | ✓ | ✓ | ✓ | ✓ (whiteboard scheduling) | — (порядок ЗАМОРОЖЕНО на початок дня, §2 правило 1 — свідомо протилежний підхід) |
| Штрихкод/сканування на цеху | ✓ (QR на матеріалах) | ✓ (tablet+barcode) | ✓ | ~ | — |
| Планшет-інтерфейс оператора | ~ | ✓ (Shop Floor tablet) | ✓ | ~ | ~ (десктоп/тач можливий, не спеціалізований) |
| QC/трасування «хто що зробив» | ✓ | ✓ (quality alerts) | ✓ (FDA-grade traceability) | ✓ | ✓ (хронологія роботи з оператором, §3) |
| Один клік / мінімум підтверджень | — (типова ERP-щільність) | — | — | — | ✓ (§2 правило 2 — свідомий продуктовий вибір) |
| Матеріальний облік (inventory) | ✓ | ✓ | ✓ | ✓ | ~ (лише колір/матеріал роботи, не складський облік) |

### 2.3 Категорія C (CRM/трекери) — лише UI-патерни, не функції домену

| Патерн | Linear | HubSpot | Jira | **KuubMill** |
|---|---|---|---|---|
| Inbox/тріаж нових елементів | ✓ (Triage) | ~ | ~ | ✓ (Нові з пошти, §9.2) |
| Збережені фільтровані «views» | ✓ | ✓ (saved board views) | ✓ (custom JQL filters) | ~ (чіпи-фільтри є, іменованих saved views нема) |
| Kanban-дошка по стадіях | ~ (списки статусів) | ✓ | ✓ (swimlanes) | — (список, не kanban — свідомо, §2 порядок фіксований) |
| Bulk-дії (масові операції) | ✓ | ~ | ✓ | — (по одній роботі; видача — по клієнту, не масово) |
| Keyboard-first (command menu, чорди) | ✓ (Cmd+K, G+letter) | — | ~ (частково) | — |

---

## 3. Що є майже у всіх (у категорії A), а в KuubMill нема

1. **Портал клієнта (self-service tracking для замовника)** — є у 10/12 систем категорії A. *Сенс для KuubMill:* низький прямо зараз — джерело 1 (лабораторія) і так дивиться Google Таблицю, джерело 2 (пошта) — це фізичні клієнти по всій країні, не інтегровані технічно; повноцінний портал — окремий проєкт, не пріоритет при 2 операторах.
2. **Інвойсинг/білінг** — є в 7/12. *Сенс:* свідомо виключено (§5: «CRM гроші не рахує»), відповідає власному рішенню, не бага.
3. **Штрихкод/QR-мітки на роботах** — є в 3-4/12 (Labtrac, Amann Girrbach RFID, частково Evident/MagicTouch). *Сенс:* спірний — §2 правило 3 явно каже «ніякого авто-зіставлення», бо спільного ключа рядок↔коронка немає фізично (коронка в лотку без бирки). Запровадити QR = змінити фізичний процес на виробництві, а не софт; варте окремого обговорення з власником, але суперечить поточній філософії «оператор звіряє оком+STL».
4. **CAD-імпорт напряму з 3Shape/exocad/iTero** — є в 6/12 (усі CAD-вендори + LabStar). *Сенс:* середній — техніки й так кладуть файли на сервер вручну (§2), пряма інтеграція з CAD зменшила б помилки набору наряду, але вимагає домовленості з лабораторією про формат/API — велика зміна процесу постачальника, не тільки KuubMill.
5. **Мобільний застосунок для техніків/кур'єрів** — є в 6/12 (MagicTouch Bench App, Route Manager; Zirkonzahn App; Amann Girrbach touchscreen). *Сенс:* низько-середній для 2 операторів — браузер на планшеті в цеху може закрити цю потребу без нативного застосунку.
6. **SMS/сповіщення клієнту про статус** — 6/12. *Сенс:* низький — клієнти категорії 2 отримують результат через кур'єра/пошту, а не стежать за статусом онлайн; окремий лист адміністраторів уже покриває терміновість (§2).
7. **QC-чеклисти (формальні quality checks)** — 5/12. *Сенс:* середній — брак і так фіксується детально (винуватець+кількість, §3), але формальний чеклист «перед видачею» (звірка кольору/форми/цілісності) міг би зменшити повернення; дешево додати як список галочок на картці роботи.
8. **Штатний облік матеріалів/складу (inventory)** — присутній майже в усіх MES (категорія B) і частково в A. *Сенс:* низький зараз — колір/матеріал фіксується на рівні роботи, а не складу; облік заготовок цирконію/дисків окремим модулем — це вже інша система (закупівлі), поза межами «одна робоча черга».

## 4. Патерни UI, які варто запозичити

1. **Inbox/Triage з клавішами навігації (J/K, E-архівувати, Shift+H-відкласти)** — Linear Triage. Пряма аналогія з KuubMill «Нові з пошти» (§9.2): зараз це список карток, можна додати J/K-навігацію + гарячі клавіші прийняття/відхилення без миші. Джерело: [Linear Docs — Triage](https://linear.app/docs/assigning-issues).
2. **Command-menu (Cmd/Ctrl+K)** для швидкого переходу між екранами (Черга/Видача/Тріаж/Статистика) і дій («взяти в роботу», «копіювати Sum3D-шлях») без миші — актуально, бо оператор і так копіює ID в Sum3D руками (§9.1). Джерело: [925studios Linear breakdown](https://www.925studios.co/blog/linear-design-breakdown-saas-ui-2026).
3. **Іменовані Saved Views (а не лише чіпи-фільтри)** — HubSpot/Jira/Linear дозволяють зберегти комбінацію фільтрів під назвою («мої на сьогодні», «прострочені термінові»). У KuubMill є чіпи (§14, «Фільтри черги на паузі»), але саме йменовані пресети — крок далі, дешевий у реалізації. Джерела: [HubSpot board views](https://insidea.com/blog/hubspot/kb/how-to-customize-board-views-for-pipelines-in-hubspot/), [Jira custom filters](https://support.atlassian.com/jira-software-cloud/docs/configure-swimlanes/).
4. **Bulk-редагування з тим самим інтерфейсом, що й одиночне** (Jira: «bulk-editing reusing the same shortcuts as editing one») — для КуубМілл могло б застосовуватись у видачі (позначити кілька робіт одного клієнта «знайдено» одним рухом), не порушуючи правило «один клік на дію» (§2). Джерело: [Jira launch notes](https://jirareleases.atlassian.com/announcements/a-faster-more-flexible-board-for-software-teams-1).
5. **Real-time dashboard зі статусом виробництва й пакувань (Argen ArgenLink)** — модель «один екран: статус кейса + трекінг посилки + причини затримки» близька до майбутньої Видачі; підтверджує напрям, що вже обраний (§9.4). Джерело: [ArgenLink](https://argen.com/link).
6. **RFID/штрихкод на бланку матеріалу (не на роботі)** — Amann Girrbach прив'язує RFID до заготовки (блок цирконію), а не до кінцевої коронки. Це обходить проблему «немає спільного ключа для готової роботи» (§2 правило 3) — можна розглянути мітку на РІВНІ ПАРТІЇ фрезерування (диск/заготовка), а не на кожній одиниці, без ламання правила «звірка оком». Джерело: [Ceramill Matik](https://www.amanngirrbach.com/en-us/equipment/production-cam/ceramill-matik/).
7. **Автоматичне заповнення полів замовлення з CAD-експорту (ArgenLink «export directly from 3Shape Dental Manager … without any data entry»)** — цільова модель для зменшення ручного набору наряду з боку лабораторії; не терміново, але напрямок, куди рухається вся індустрія. Джерело: [LMTmag ArgenLink](https://lmtmag.com/articles/9-software-innovations-argen-digital-launches-argenlink).
8. **QC worksheet на планшеті перед випуском (Odoo Manufacturing tablet quality checks)** — легкий чеклист «перевір колір/форму/тріщини» безпосередньо в момент «знайдено» на екрані Видачі, без окремого модуля. Джерело: [Odoo Manufacturing docs](https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/manufacturing.html).
9. **Технік-портал з нотифікаціями про зміни в кейсі (MagicTouch Technician Bench App)** — якщо колись знадобиться повідомляти техніків лабораторії про статус (без повного порталу клієнта), формат «легкий read-only app для одного повідомлення» дешевший за повний портал. Джерело: [Magic Touch DLCPM Enterprise](https://magictouchsoftware.com/dlcpm-enterprise/).
10. **Whiteboard-scheduling з видимими вузькими місцями (JobBOSS² «see bottlenecks before they arise»)** — концептуально цікаво для майбутньої «Смуги печей» (§14): візуалізація завантаження трьох пічок наперед, а не лише поточний стан. Джерело: [ECI JobBOSS² features](https://www.ecisolutions.com/products/jobboss2/features/).

## 5. Що KuubMill робить, чого немає на ринку (за результатами пошуку)

- **VNC-OCR моніторинг реальних печей (read-only, голосування трьох сигналів, glyph-навчання на власних кадрах)** — жодна з досліджених dental-LMS чи MES-систем не інтегрується з чужим обладнанням через відеовихід VNC і оптичне розпізнавання цифр. Amann Girrbach/Zirkonzahn пропонують «планування завантаження», але лише для ВЛАСНОГО обладнання з нативним протоколом; тут — інтеграція «знизу», без співпраці з виробником пічки. (§14 «Печі (VNC, лише читання)»).
- **STL-звірка «оком» як головний інструмент видачі, без штрихкодів** — це свідома відмова від патерну штрихкодів/RFID, які є галузевим стандартом (Labtrac, Amann Girrbach). Жодна з переглянутих систем не описує повноекранний STL-прев'ю САМЕ як заміну штрихкоду для фінальної звірки перед видачею.
- **Дворівнева «готовність» без явного статусного поля** (job_code + порожній sum3d_id = «можна брати», §5) — інші системи використовують явний статус-enum; тут — похідна логіка з двох існуючих таблично-орієнтованих полів, збережена для сумісності з живою Google Таблицею.
- **Двонапрямна синхронізація з живою Google Таблицею, яку продовжують редагувати вручну** (§3, §14 «Синк таблиці») — переважна більшість dental-LMS замінює таблицю повністю; жодна не описана як «працює паралельно і чекає, поки таблицю приберуть» з захисними механізмами від масової архівації через обрізане читання проксі.
- **Нечітке зіставлення імені клієнта (rapidfuzz) для видачі товару без спільного ключа** (§4) — це рішення конкретно під «дезорганізовані» вхідні дані (одруківки в іменах), тоді як ринкові системи покладаються на структуровані форми замовлення від початку.
- **Копії Google-таблиці (сирі CSV-знімки вкладок для аварійного відновлення)** (§14 «Копії Google-таблиці») — вузькоспеціалізована страховка від втрати єдиного джерела правди, якої не існує в системах, що самі є джерелом правди.
- **IMAP-тріаж пошти з розпізнаванням матеріалу/кольору з вільного тексту + фільтри «не наша робота»** (§14 «Пошта», «Матеріали vs винятки») — жодна з переглянутих LMS не описує вбудований email-парсинг довільних листів як джерело замовлень; є лише структуровані клієнтські портали (протилежний підхід).

---

## Список усіх джерел

- [Evident — Lab Management Solutions](https://www.evidentdigital.com/smart-lab/lab-management-solutions)
- [Evident — EviSmart CaseEntry](https://www.evidentdigital.com/blog/evident-launches-evi-smart-case-entry-to-eliminate-manual-case-entry-for-dental-labs)
- [Labtrac — Features](https://www.labtrac.com/Features)
- [Labtrac — Features (online)](https://www.labtrac.online/en/Home/Features)
- [Magic Touch — DLCPM Enterprise](https://magictouchsoftware.com/dlcpm-enterprise/)
- [Magic Touch — Lab Connex](https://magictouchsoftware.com/lab-connex/)
- [LMTmag — LabStar](https://lmtmag.com/labstar)
- [SoftwareSuggest — LabStar](https://www.softwaresuggest.com/labstar)
- [iLab — dental LMS](https://ilab.dental/)
- [GreatLab — Lab Management Software](https://greatlab.cloud/lab-management-software/)
- [Dental Lab Guru](https://www.dentallabguru.com/)
- [3Shape — Lab Management Software](https://www.3shape.com/en-us/software/lms)
- [3Shape — LMS integration requirements](https://support.3shape.com/lab-dental-system-setup-how-to/what-is-required-for-integrating-lab-management-system-with-3shape-dental-system)
- [exocad — dentalshare](https://exocad.com/our-products/dentalshare/)
- [Zirkonzahn — CAD/CAM Software](https://zirkonzahn.com/us/cad-cam-systems/cad-cam-software)
- [Amann Girrbach — Software catalog](https://www.amanngirrbach.com/en-us/catalog/software/)
- [Amann Girrbach — Ceramill Matik](https://www.amanngirrbach.com/en-us/equipment/production-cam/ceramill-matik/)
- [Argen — ArgenLink](https://argen.com/link)
- [LMTmag — Argen Digital launches ArgenLink](https://lmtmag.com/articles/9-software-innovations-argen-digital-launches-argenlink)
- [Fulcrum — Manufacturing Software](https://fulcrumpro.com/)
- [Fulcrum — Job Tracking](https://fulcrumpro.com/manufacturing-software/job-tracking)
- [Odoo — Manufacturing (MRP)](https://www.odoo.com/app/manufacturing)
- [Odoo 19 — Manufacturing docs](https://www.odoo.com/documentation/19.0/applications/inventory_and_mrp/manufacturing.html)
- [ProShop ERP — Product](https://proshoperp.com/product/)
- [ProShop ERP — Traceability blog](https://proshoperp.com/blog/achieving-full-traceability-shop-floor/)
- [ECI JobBOSS² — Features](https://www.ecisolutions.com/products/jobboss2/features/)
- [Linear Docs — Assigning issues / Triage](https://linear.app/docs/assigning-issues)
- [925studios — Linear design breakdown](https://www.925studios.co/blog/linear-design-breakdown-saas-ui-2026)
- [fastshortcuts.com — Linear shortcuts](https://fastshortcuts.com/shortcuts/linear/)
- [HubSpot Knowledge — Sales workspace deals](https://knowledge.hubspot.com/prospecting/create-and-manage-deals-in-the-sales-workspace)
- [INSIDEA — Customize board views for pipelines](https://insidea.com/blog/hubspot/kb/how-to-customize-board-views-for-pipelines-in-hubspot/)
- [Atlassian — Configure swimlanes](https://support.atlassian.com/jira-software-cloud/docs/configure-swimlanes/)
- [Jira — Launch notes: faster, more flexible board](https://jirareleases.atlassian.com/announcements/a-faster-more-flexible-board-for-software-teams-1)
- [dentalmillingcentersoftware.com](https://dentalmillingcentersoftware.com/)

# Локальные ресурсы

- `fonts/GolosText.ttf`: Google Fonts, семейство Golos Text; лицензия `GolosText-OFL.txt`.
- `fonts/Noto Sans Regular/*.pbf`: глифы Noto Sans из OpenFreeMap; лицензия `NotoSans-OFL.txt`.
- `map/reference-tracks.geojson`: опорная геометрия трамвайных путей из `src/data/reference-network.json`; источник и хеш записаны в `map/manifest.json`, ODbL 1.0.
- `docs/swagger-ui-bundle.js`, `docs/swagger-ui.css`: swagger-ui-dist 5.33.0, Apache-2.0; лицензия `docs/LICENSE`.

Все файлы обслуживаются приложением по `/assets`. Карта не загружает уличные тайлы и не требует доступа к картографическому провайдеру. Датированные маршруты и остановки обновляются через API.

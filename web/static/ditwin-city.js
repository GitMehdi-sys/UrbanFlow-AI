/* Ditwin city context. OpenFreeMap/OSM building geometry and city camera. */
(() => {
  const escMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const element = (tag, text, className) => {
    const e = document.createElement(tag); e.textContent = text;
    if (className) e.className = className; return e;
  };
  function buildings() {
    if (!map.getSource('ditwin-buildings')) map.addSource('ditwin-buildings', {
      type: 'vector', url: 'https://tiles.openfreemap.org/planet',
      attribution: '© OpenStreetMap contributors · OpenFreeMap'
    });
    if (!map.getLayer('ditwin-buildings-3d')) map.addLayer({
      id: 'ditwin-buildings-3d', source: 'ditwin-buildings', 'source-layer': 'building',
      type: 'fill-extrusion', minzoom: 13, filter: ['all', ['!=', ['get', 'hide_3d'], true], ['>', ['coalesce', ['get', 'render_height'], 0], 0]],
      paint: {
        'fill-extrusion-color': ['interpolate', ['linear'], ['coalesce', ['get', 'render_height'], 0], 0, '#c4d7d3', 12, '#8bada8', 35, '#3f7778'],
        'fill-extrusion-height': ['coalesce', ['get', 'render_height'], 0],
        'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0],
        'fill-extrusion-opacity': 0.88
      }
    });
  }
  map.on('style.load', buildings);
  map.once('load', buildings);
  if (map.isStyleLoaded()) buildings();
  window.focusDitwinCity = () => {
    map.flyTo({center: [-13.2033, 27.1536], zoom: 15.5, pitch: 58, bearing: -22, duration: escMotion.matches ? 0 : 1100});
    document.getElementById('perspective-label').textContent = 'Plan view';
  };
  map.on('click', 'ditwin-buildings-3d', event => {
    if (!event.features?.length) return;
    const properties = event.features[0].properties;
    const box = document.createElement('div'); box.className = 'ditwin-building-popup';
    box.append(element('strong', properties.name || 'Mapped building'),
      element('p', 'OpenStreetMap footprint. The displayed height is a visualization and may be estimated.'));
    const popup = new mapboxgl.Popup({maxWidth: '260px'}).setLngLat(event.lngLat).setDOMContent(box).addTo(map);
  });
  map.on('mouseenter', 'ditwin-buildings-3d', () => map.getCanvas().style.cursor = 'pointer');
  map.on('mouseleave', 'ditwin-buildings-3d', () => map.getCanvas().style.cursor = '');
})();

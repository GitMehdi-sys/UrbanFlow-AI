/* Water planning uses explicitly imported measurements. */
(() => {
  const byId = id => document.getElementById(id);
  let chart;
  const status = message => { byId('water-status').textContent = message; };
  async function api(path, options) {
    const response = await fetch(`/api/water/${path}`, options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Invalid measurements or scenario. Check the documented schema and field ranges.');
    return data;
  }
  function displaySource(data) {
    byId('water-source').textContent = `${data.source} · Observed ${data.observed_at} · Production ${data.production_m3_day} m³/day · Demand ${data.demand_m3_day} m³/day · Storage ${data.storage_m3}/${data.storage_capacity_m3} m³`;
    byId('water-run').disabled = false;
  }
  window.openWaterPanel = async () => {
    byId('city-ops-panel').hidden = false;
    try { displaySource(await api('measurements')); status('Measurements loaded. Choose scenario settings.'); }
    catch (error) { status(error.message); }
  };
  window.closeWaterPanel = () => { byId('city-ops-panel').hidden = true; };
  byId('water-import').onclick = async () => {
    const file = byId('water-file').files[0];
    if (!file) return status('Choose a measurements JSON file. See README for the schema.');
    if (file.size > 32768) return status('Measurements file must be smaller than 32 KB.');
    byId('water-import').disabled = true;
    try {
      const data = JSON.parse(await file.text());
      displaySource(await api('measurements', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}));
      chart?.destroy(); chart = null; byId('water-results').replaceChildren();
      status('Measurements imported. Calculate a forecast to see results.');
    } catch(error) { status(error.message); }
    finally { byId('water-import').disabled = false; }
  };
  byId('water-run').onclick = async () => {
    const ids = ['water-horizon','water-loss','water-reduction'];
    if (ids.some(id => !byId(id).reportValidity())) return;
    byId('water-run').disabled = true; status('Calculating forecast…');
    try {
      const data = await api('forecast', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({horizon_hours:Number(byId(ids[0]).value),production_loss_percent:Number(byId(ids[1]).value),demand_reduction_percent:Number(byId(ids[2]).value)})});
      chart?.destroy();
      const result = byId('water-results'); result.replaceChildren();
      const summary = document.createElement('p'), last = data.timeline.at(-1);
      summary.textContent = `Final storage: ${last.storage_m3} m³. Unmet demand: ${last.unmet_m3} m³. Overflow: ${last.overflow_m3} m³. ${data.method}`;
      const box = document.createElement('div'); box.className='water-chart-box';
      const canvas = document.createElement('canvas'); canvas.setAttribute('aria-label','Forecast storage in cubic metres by hour'); canvas.setAttribute('role','img');
      box.append(canvas); result.append(summary,box);
      chart = new Chart(canvas,{type:'line',data:{labels:data.timeline.map(p=>p.hour),datasets:[{label:'Storage (m³)',data:data.timeline.map(p=>p.storage_m3),borderColor:'#0f766e',pointRadius:0}]},options:{responsive:true,maintainAspectRatio:false,scales:{y:{min:0},x:{title:{display:true,text:'Hours'}}}}});
      status(`Forecast calculated from ${data.measurements.source}, observed ${data.measurements.observed_at}.`);
    } catch(error) { status(error.message); }
    finally { byId('water-run').disabled = false; }
  };
  ['water-horizon','water-loss','water-reduction'].forEach(id => byId(id).addEventListener('input',() => { chart?.destroy(); chart=null; byId('water-results').replaceChildren(); status('Settings changed. Calculate again to update results.'); }));
})();

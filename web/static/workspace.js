/* UI state joins existing geographic, chat, dashboard and replay integrations. */
const metricUnits={density:'veh/km',laneDensity:'veh/km/lane',speed:'m/s',occupancy:'%',waitingTime:'s',timeLoss:'s',traveltime:'s',sampledSeconds:'vehicle·s'};
function onRecordedHeatmapClick(event){
    if(!event.features?.length)return;
    const props=event.features[0].properties;
    const content=document.createElement('div');
    const title=document.createElement('strong');title.textContent=props.name||props.id;
    const value=document.createElement('div');value.textContent=`${props.attribute}: ${Number(props.value).toFixed(2)} ${metricUnits[props.attribute]||''}`;
    content.append(title,value);
    new mapboxgl.Popup().setLngLat(event.lngLat).setDOMContent(content).addTo(map);
}
function onRecordedHeatmapEnter(){map.getCanvas().style.cursor='pointer';}
function onRecordedHeatmapLeave(){map.getCanvas().style.cursor='';}
async function apiJSON(url, options = {}) {
    const response = await fetch(url, {...options, signal: options.signal || AbortSignal.timeout(30000)});
    if (!response.ok) {
        let body = {};
        try { body = await response.clone().json(); } catch (_) { /* preserve status */ }
        throw new Error(body.detail || body.error || `Request failed (${response.status})`);
    }
    return response;
}

function setMapState(text) {
    const state = document.getElementById('map-state');
    state.replaceChildren();
    const icon = document.createElement('i');
    icon.className = 'fas fa-circle-check';
    state.append(icon, document.createTextNode(text));
}

function fitNetwork() {
    if (!currentNetworkGeoJSON) { showToast('Select a road network first.', 'info'); return; }
    const b = calculateBboxFromGeoJSON(currentNetworkGeoJSON);
    if (b) map.fitBounds([[b.west,b.south],[b.east,b.north]], {padding:{top:innerWidth<=760?245:175,bottom:80,left:35,right:35}, maxZoom:17, pitch:0, bearing:0, duration:reducedMotion.matches?0:1000});
}

function togglePerspective() {
    const tilted = map.getPitch() < 10;
    map.easeTo({pitch:tilted?45:0,bearing:tilted?-15:0,duration:reducedMotion.matches?0:750});
    document.getElementById('perspective-label').textContent=tilted?'Plan view':'Tilt view';
}

function togglePanel(which) {
    const panel = document.getElementById(which === 'assets' ? 'assets-panel' : 'analysis-panel');
    const drawer = window.innerWidth <= (which === 'assets' ? 1050 : 760);
    if (drawer) {
        const open = !panel.classList.contains('drawer-open');
        document.querySelectorAll('.drawer-open').forEach(p => p.classList.remove('drawer-open'));
        panel.classList.toggle('drawer-open', open);
        document.getElementById('panel-scrim').classList.toggle('visible', open);
        document.querySelector('.main-content').inert=open;
        if(open)panel.querySelector('button,input,select,textarea,[tabindex]')?.focus();
    } else {
        document.body.classList.toggle(which === 'assets' ? 'assets-collapsed' : 'assistant-collapsed');
    }
    syncPanelButtons();
}

function closeDrawers() {
    document.querySelectorAll('.drawer-open').forEach(p => p.classList.remove('drawer-open'));
    document.getElementById('panel-scrim').classList.remove('visible');
    document.querySelector('.main-content').inert=false;
    syncPanelButtons();
}

function syncPanelButtons() {
    for (const [which, id, breakpoint] of [['assets','assets-panel',1050],['assistant','analysis-panel',760]]) {
        const panel = document.getElementById(id);
        const visible = innerWidth <= breakpoint ? panel.classList.contains('drawer-open') : !document.body.classList.contains(`${which}-collapsed`);
        document.querySelector(`[aria-controls="${id}"]`).setAttribute('aria-expanded', String(visible));
    }
}

function ensureAnalysisPanel() {
    if (innerWidth <= 760) {
        if (!document.getElementById('analysis-panel').classList.contains('drawer-open')) togglePanel('assistant');
    } else document.body.classList.remove('assistant-collapsed');
}

const layerGroups = {network:['network-layer'], boundary:['network-bbox-layer'], results:['heatmap-layer','compare-heatmap-layer'], base:['base-raster']};
function toggleMapLayer(group, visible) {
    let ids = layerGroups[group];
    if (group === 'base' && MAPBOX_TOKEN) ids = map.getStyle().layers.filter(l => !l.source || ['composite','mapbox-dem'].includes(l.source)).map(l=>l.id);
    for (const id of ids) if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible?'visible':'none');
    updateLayerAvailability();
}

function updateLayerAvailability() {
    let count = 0;
    for (const [group, ids] of Object.entries(layerGroups)) {
        const input = document.getElementById(`layer-${group}`);
        const available = group === 'base' || ids.some(id => map.getLayer(id));
        input.disabled = !available;
        input.closest('label').style.opacity = available ? '1' : '.55';
        if (available && input.checked) count++;
    }
    document.getElementById('layer-count').textContent = `${count} active`;
}

function updateNetworkContext(data) {

    document.getElementById('canvas-network-name').textContent = currentLoadedNetwork.replace('.net.xml','').replaceAll('_',' ');
    document.getElementById('asset-network-name').textContent = currentLoadedNetwork;
    try { localStorage.setItem('urbanflow-network', currentLoadedNetwork); } catch (_) { /* storage is optional */ }
    document.getElementById('network-summary').textContent = `${data.edge_count.toLocaleString()} road segments · EPSG:4326`;
    document.querySelector('.asset-intro p').textContent='Georeferenced road network loaded. Select a road to inspect its properties.';
    setMapState('Network loaded · select a road to inspect');
    document.getElementById('map-provenance').textContent = data.modified ? `Network file · ${new Date(data.modified).toLocaleDateString()}` : 'Imported network · recorded data';
    clearInspector();
    for (const group of ['network','boundary']) toggleMapLayer(group, document.getElementById(`layer-${group}`).checked);
    closeDrawers();
}

function inspectFeature(feature) {
    const props = feature.properties;
    document.getElementById('feature-name').textContent = props.name || props.id;
    const box = document.getElementById('feature-properties');
    box.replaceChildren();
    for (const [label,value] of [['Segment ID',props.id],['Classification',props.type || 'Not supplied'],['Lanes',props.lanes],['Speed limit',`${(Number(props.speed)*3.6).toFixed(1)} km/h`],['Length',`${Number(props.length).toFixed(1)} m`]]) {
        const row = document.createElement('div'); row.className='property-row';
        const text=document.createElement('span');text.textContent=label;
        const strong=document.createElement('strong');strong.textContent=value;
        row.append(text,strong);box.append(row);
    }
    document.getElementById('feature-inspector').hidden=false;
    const road=currentNetworkGeoJSON?.features.find(f=>f.properties.id===props.id);
    if (!road) return;
    if (map.getSource('inspected-road')) map.getSource('inspected-road').setData(road);
    else {
        map.addSource('inspected-road',{type:'geojson',data:road});
        map.addLayer({id:'inspected-road-glow',type:'line',source:'inspected-road',paint:{'line-color':'#2563eb','line-width':12,'line-opacity':.18}});
        map.addLayer({id:'inspected-road-line',type:'line',source:'inspected-road',paint:{'line-color':'#2563eb','line-width':4}});
    }
}

function clearInspector() {
    document.getElementById('feature-inspector').hidden=true;
    for (const id of ['inspected-road-line','inspected-road-glow']) if (map.getLayer(id)) map.removeLayer(id);
    if(map.getSource('inspected-road'))map.removeSource('inspected-road');
}

function resetNetworkContext(){
    stopReplay();clearInspector();
    for(const id of ['network-bbox-layer'])if(map.getLayer(id))map.removeLayer(id);
    if(map.getSource('network-bbox-source'))map.removeSource('network-bbox-source');
    currentLoadedNetwork=null;currentNetworkGeoJSON=null;
    try{localStorage.removeItem('urbanflow-network');}catch(_){}
    document.getElementById('canvas-network-name').textContent='Explore your urban network';
    document.getElementById('asset-network-name').textContent='No network selected';
    document.getElementById('network-summary').textContent='Select a network to begin your study';
    document.querySelector('.asset-intro p').textContent='Import a georeferenced network to explore its roads and run a scenario.';
    document.getElementById('map-provenance').textContent='Geographic context · not a live traffic feed';
    setMapState('Map ready · select or import a network');updateLayerAvailability();
}

// Preserve actual overlays across a basemap change; setStyle otherwise drops them.
let pendingOverlays = null;
function changeBasemap(style) {
    if (replayPlaying) stopPlayback();
    const previous = map.getStyle();
    const sourceIds = Object.keys(previous.sources).filter(id=>!['basemap','composite','mapbox-dem'].includes(id));
    pendingOverlays={sources:Object.fromEntries(sourceIds.map(id=>[id,previous.sources[id]])),layers:previous.layers.filter(l=>sourceIds.includes(l.source))};
    document.querySelector('.main-content').classList.toggle('dark-map',mapStyles[currentStyleIndex].label==='Dark');
    map.setStyle(style,{diff:false});
}

async function importAsset(input) {
    const file=input.files[0];if(!file)return;
    try {
        const data=new FormData();data.append('file',file);
        const result=await (await apiJSON('/api/upload/asset',{method:'POST',body:data})).json();
        showToast(`Imported ${result.filename}`,'success');
        await loadFileTree();
        if(result.kind==='networks')await loadNetwork(result.filename);
    }catch(error){showToast(error.message,'error');}
    finally{input.value='';}
}

async function openScenario() {
    const dialog=document.getElementById('scenario-dialog');
    const notice=document.getElementById('scenario-notice');
    notice.textContent='Loading scenario assets…';
    if(!dialog.open)dialog.showModal();
    document.getElementById('scenario-submit').disabled=true;
    try{
        const [assets,health]=await Promise.all([(await apiJSON('/api/scenario/assets')).json(),(await apiJSON('/api/health')).json()]);
        for(const [id,items,empty]of [['scenario-network',assets.networks,'Import a .net.xml network'],['scenario-demand',assets.demand,'Import a .rou.xml or .trips.xml file']]){
            const select=document.getElementById(id);select.replaceChildren();
            for(const name of items.length?items:['']){const option=document.createElement('option');option.value=name;option.textContent=name||empty;select.append(option);}
        }
        if(currentLoadedNetwork)document.getElementById('scenario-network').value=currentLoadedNetwork;
        const ready=assets.networks.length&&assets.demand.length&&health.simulation.ready&&!health.simulation.busy;
        document.getElementById('scenario-submit').disabled=!ready;
        notice.textContent=!health.simulation.ready?'SUMO is unavailable. Configure SUMO_HOME or PATH.':health.simulation.busy?'A simulation is already running. Check Runs for progress.':!assets.networks.length||!assets.demand.length?'Import a georeferenced network and its matching demand file using Scenario Assets.':'Demand must reference roads in the selected network. This creates a new recorded run.';
    }catch(error){notice.textContent=error.message;}
}

async function submitScenario(event){
    event.preventDefault();const button=document.getElementById('scenario-submit');button.disabled=true;
    try{
        const body={label:document.getElementById('scenario-label').value,network:document.getElementById('scenario-network').value,demand:document.getElementById('scenario-demand').value,duration:Number(document.getElementById('scenario-duration').value)};
        await apiJSON('/api/simulation/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        document.getElementById('scenario-dialog').close();ensureAnalysisPanel();switchSidebarTab('operations');
        showToast('SUMO job started. Recorded outputs will appear in Runs.','success');
        scheduleJobRefresh();
    }catch(error){document.getElementById('scenario-notice').textContent=error.message;}
    finally{button.disabled=false;}
}

let jobRefreshTimer;
function scheduleJobRefresh(){clearTimeout(jobRefreshTimer);jobRefreshTimer=setTimeout(async()=>{await loadOperations();await refreshReadiness();},2000);}

const loadAgentOperations=loadOperations;
loadOperations=async function(){
    await loadAgentOperations();
    try{
        const data=await(await apiJSON('/api/simulation/jobs')).json();
        const target=document.getElementById('operations-content');
        if(!data.jobs.length)return;
        if(target.classList.contains('dash-empty'))target.replaceChildren();
        target.classList.remove('dash-empty');
        const holder=document.createElement('div');holder.className='recorded-jobs';
        for(const job of data.jobs){
            const article=document.createElement('article');article.className='operation-card';
            const meta=document.createElement('div');meta.className='operation-meta';
            const status=document.createElement('span');status.className=`operation-status ${job.status}`;status.textContent=job.status;
            const when=document.createElement('span');when.textContent=new Date(job.created_at).toLocaleString();meta.append(status,when);
            const title=document.createElement('div');title.className='operation-request';title.textContent=job.label||'Road simulation';
            const detail=document.createElement('div');detail.className='job-details';detail.textContent=`${job.network} · ${job.duration} s · ${job.demand}`;
            article.append(meta,title,detail);
            if(job.error||job.analysis_error){const error=document.createElement('div');error.className='job-error';error.textContent=job.error||job.analysis_error;article.append(error);}
            if(job.status==='completed'){
                const actions=document.createElement('div');actions.className='job-actions';
                const add=(label,fn)=>{const b=document.createElement('button');b.textContent=label;b.onclick=fn;actions.append(b);};
                if(job.replay&&job.replay_available)add('Replay',async()=>{closeDrawers();await loadNetwork(job.network);document.getElementById('replay-controls').classList.add('active');await refreshReplayList();document.getElementById('replay-scenario-select').value=job.replay;await loadReplayScenario(job.replay);});
                if(job.netstate&&job.heatmap_available)add('Heatmap',async()=>{closeDrawers();await loadNetwork(job.network);await loadHeatmap(job.network,job.netstate,'density');});
                if(job.analysis_status==='success'&&job.analysis_available)add('Analyze',async()=>{await switchSidebarTab('dashboard');document.getElementById('dash-sim-a').value=job.simulation_id;await loadDashboard();});
                if(!actions.children.length){const note=document.createElement('div');note.className='job-details';note.textContent='Outputs are no longer in this workspace.';article.append(note);}
                article.append(actions);
            }
            holder.append(article);
        }
        target.prepend(holder);
        if(data.jobs.some(j=>['queued','running'].includes(j.status)))scheduleJobRefresh();
    }catch(error){const p=document.createElement('p');p.className='job-error';p.textContent=error.message;document.getElementById('operations-content').prepend(p);}
};

async function refreshReadiness(){
    try{
        const health=await(await apiJSON('/api/health')).json();window.workspaceHealth=health;
        const status=document.getElementById('system-status');status.classList.add('ready');
        status.textContent=health.simulation.ready?'SUMO available':'Workspace available';
        const assistant=document.getElementById('assistant-readiness');assistant.classList.toggle('available',health.ai.enabled);
        assistant.textContent=health.ai.configured?`${health.ai.provider} configured · connectivity checked on send`:`AI unavailable · configure ${health.ai.required_variable} in .env`;
        document.getElementById('send-btn').disabled=!health.ai.enabled||isThinking;
    }catch(error){document.getElementById('system-status').classList.remove('ready');document.getElementById('system-status').textContent='API unavailable';document.getElementById('assistant-readiness').textContent='Backend unavailable. Check the server and retry.';}
}

// Render filenames as text nodes and bind handlers without inline HTML.
loadFileTree=async function(){
    const box=document.getElementById('file-tree');
    try{
        const data=await(await apiJSON('/api/files')).json();
        const render=(items,parent)=>{for(const item of items){
            if(item.type==='folder'){
                const section=document.createElement('details');section.open=item.expanded||['networks','trips'].includes(item.name);
                const summary=document.createElement('summary');summary.className='tree-item';summary.textContent=`${item.name}  (${item.children.length})`;section.append(summary);
                const children=document.createElement('div');children.style.paddingLeft='10px';render(item.children,children);section.append(children);parent.append(section);
            }else{
                const button=document.createElement('button');button.className='tree-item';button.type='button';button.style.cssText='width:100%;border:0;background:transparent;display:flex;text-align:left;gap:7px';button.title=item.name;
                const icon=document.createElement('i');icon.className=`fas ${item.name.endsWith('.net.xml')?'fa-route':'fa-file-code'}`;
                const name=document.createElement('span');name.className='name';name.textContent=item.name;button.append(icon,name);
                button.onclick=()=>{document.querySelectorAll('.tree-item.active').forEach(e=>e.classList.remove('active'));button.classList.add('active');if(item.name.endsWith('.net.xml'))loadNetwork(item.name);else if(item.type==='report')showReportInline(item.path.replaceAll('\\','/'),item.name);else showToast('Asset available for scenario setup or recorded output.','info');};parent.append(button);
            }
        }};
        box.replaceChildren();if(data.tree?.length)render(data.tree,box);
        else{box.innerHTML='<div class="asset-empty"><i class="far fa-folder-open"></i>Your scenario starts here.<br>Import a network and matching demand.</div>';}
    }catch(error){box.textContent=error.message;}
};
toggleNetworkDropdown=async function(){
    const dropdown=document.getElementById('network-dropdown');dropdown.classList.toggle('active');
    if(!dropdown.classList.contains('active'))return;
    dropdown.textContent='Loading networks…';
    try{const data=await(await apiJSON('/api/networks')).json();dropdown.replaceChildren();
        if(!data.networks.length)dropdown.textContent='Import a georeferenced .net.xml in Scenario Assets.';
        for(const name of data.networks){const b=document.createElement('button');b.className='network-item';b.textContent=name;b.onclick=e=>{e.stopPropagation();dropdown.classList.remove('active');loadNetwork(name);};dropdown.append(b);}
    }catch(error){dropdown.textContent=error.message;}
};

// Sanitize provider Markdown while retaining the existing structured tool widgets.
const originalMarkdownParse=marked.parse.bind(marked);
marked.parse=(text,options)=>DOMPurify.sanitize(originalMarkdownParse(text,options));
const scrim=document.createElement('button');scrim.id='panel-scrim';scrim.className='panel-scrim';scrim.setAttribute('aria-label','Close side panel');scrim.onclick=closeDrawers;document.body.append(scrim);
document.addEventListener('keydown',event=>{if(event.key==='Escape'){closeDrawers();clearInspector();document.getElementById('network-dropdown').classList.remove('active');}});
window.addEventListener('resize',()=>{closeDrawers();map.resize();});
new ResizeObserver(()=>map.resize()).observe(document.querySelector('.main-content'));
document.getElementById('chat-input').addEventListener('input',event=>{event.target.style.height='auto';event.target.style.height=Math.min(event.target.scrollHeight,130)+'px';});
map.on('mousemove',event=>{const p=event.lngLat;document.getElementById('map-coordinates').textContent=`${Math.abs(p.lat).toFixed(4)}° ${p.lat>=0?'N':'S'} / ${Math.abs(p.lng).toFixed(4)}° ${p.lng>=0?'E':'W'}`;});
map.on('error',event=>{const box=document.getElementById('map-error');box.hidden=false;box.textContent=`Map provider error: ${event.error?.message||'Unable to load map resources'}. Check network access${MAPBOX_TOKEN?' and your public Mapbox token':''}.`;setMapState('Map resources unavailable');});
map.on('sourcedata',event=>{if(event.sourceId==='basemap'&&event.isSourceLoaded){document.getElementById('map-error').hidden=true;setMapState(currentNetworkGeoJSON?'Network loaded · select a road to inspect':'Map ready · select or import a network');}});
map.on('style.load',()=>{
    if(pendingOverlays){const saved=pendingOverlays;pendingOverlays=null;if(!saved.layers.some(l=>l.id==='network-layer')&&map.getLayer('network-layer')){map.removeLayer('network-layer');map.removeSource('network-source');}for(const[id,source]of Object.entries(saved.sources))if(!map.getSource(id))map.addSource(id,source);for(const layer of saved.layers){if(!map.getLayer(layer.id))map.addLayer(layer);else map.setLayoutProperty(layer.id,'visibility',layer.layout?.visibility||'visible');}}
    for(const group of Object.keys(layerGroups))toggleMapLayer(group,document.getElementById(`layer-${group}`).checked);
    updateLayerAvailability();
});
map.on('idle',updateLayerAvailability);
map.on('movestart',()=>map.getContainer().setAttribute('aria-busy','true'));
map.on('idle',()=>map.getContainer().setAttribute('aria-busy','false'));
const mapInitTimeout=setTimeout(()=>{if(!map.loaded()){document.getElementById('map-error').hidden=false;document.getElementById('map-error').textContent='Map initialization is taking longer than expected. Check WebGL support and access to the configured tile provider.';}},20000);
map.once('load',()=>clearTimeout(mapInitTimeout));
map.once('load',async()=>{
    let saved;try{saved=localStorage.getItem('urbanflow-network');}catch(_){return;}
    if(saved){try{const data=await(await apiJSON('/api/networks')).json();if(data.networks.includes(saved))await loadNetwork(saved);}catch(error){showToast(error.message,'error');}}
});
syncPanelButtons();refreshReadiness();

function setRoadDuration(seconds){document.getElementById('scenario-duration').value=seconds;}

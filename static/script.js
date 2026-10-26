'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const DAYS = ['월', '화', '수', '목', '금', '토', '일'];
const ADMIN_PAGE = location.pathname.replace(/\/+$/, '') === '/admin';
let auth, data, homeData, schoolResults = [], mutationBusy = false;
let viewWeek = 1, comciganSchools = [], importTicket = null;
let studentRows = [];
const mealJobs = new Map();
let classChangeTimer = null, refreshSequence = 0;

const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
function notice(message, error=false) {
  $('#notice').textContent = message;
  $('#notice').classList.toggle('error', error);
}
async function api(url, method='GET', body, signal) {
  const options = {method, headers: {}, signal};
  if (method !== 'GET') options.headers['X-CSRF-Token'] = auth?.csrf || '';
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && !url.startsWith('/api/login/')) await showSession();
    throw Error(result.error || '요청에 실패했습니다.');
  }
  return result;
}
async function run(task, mutation=false) {
  if (mutation && mutationBusy) return;
  if (mutation) {
    mutationBusy = true;
    $$('button[type="submit"]').forEach(b => b.disabled = true);
    $$('#view-grade, #view-class, #view-week').forEach(input => input.disabled = true);
  }
  try { await task(); }
  catch (error) { notice(error.message || '처리하지 못했습니다.', true); }
  finally {
    if (mutation) {
      mutationBusy = false;
      $$('button[type="submit"]').forEach(b => b.disabled = false);
      $$('#view-grade, #view-class, #view-week').forEach(input => input.disabled = false);
    }
  }
}
function isAdmin() { return auth?.user?.role === 'admin'; }
async function showSession() {
  auth = await api('/api/session');
  if (auth.version && parseFloat(auth.version) < 2.7) throw Error('서버가 이전 버전입니다. 관리자에게 서버 재시작을 요청하세요.');
  const loggedIn = !!auth.user;
  $('#auth-area').hidden = loggedIn;
  $('#application').hidden = !loggedIn;
  $('#navigation').hidden = !loggedIn;
  $('#logout').hidden = !loggedIn;
  $('#students-nav').hidden = !isAdmin();
  $('#admin-setup').hidden = auth.admin_ready || !ADMIN_PAGE;
  $('#account').textContent = loggedIn
    ? `${auth.user.login} ${auth.user.name} · ${isAdmin() ? '관리자' : '학생'}`
    : '가입 승인 후 이용';
  if (!loggedIn) {
    $('#title').textContent = ADMIN_PAGE ? '관리자 로그인' : '학생 로그인';
    $('#login-heading').textContent = ADMIN_PAGE ? '관리자 전용 로그인' : '학생 로그인';
    $('#login-label').textContent = ADMIN_PAGE ? '관리자 ID' : '학번';
    $('#login-tag').textContent = ADMIN_PAGE ? 'ADMIN LOGIN' : 'STUDENT LOGIN';
    $('#login-id').placeholder = ADMIN_PAGE ? '관리자 ID 입력' : '예: 10101';
    $('#registration-card').hidden = ADMIN_PAGE;
    $('#auth-heading').textContent = ADMIN_PAGE ? '학교의 하루를 관리하는 공간.' : '우리 학교의 하루, 내 계정으로.';
    $('#auth-description').textContent = ADMIN_PAGE ? '학생 가입 승인, 학교 등록, 시간표 편집은 관리자만 할 수 있습니다.' : '학번과 이름으로 가입 신청하고, 관리자 승인 후 로그인하세요.';
    $('#login-help').textContent = ADMIN_PAGE ? '관리자 계정은 서버의 터미널에서 처음 한 번 생성합니다.' : '아직 승인되지 않았다면 관리자에게 학번과 이름을 확인해 달라고 요청하세요.';
    for (const job of mealJobs.values()) job.abort();
    mealJobs.clear();
    return false;
  }
  $('#school-editor').hidden = !isAdmin();
  $('#comcigan-editor').hidden = !isAdmin();
  $('#timetable-actions').hidden = !isAdmin();
  $('#timetable-guide').textContent = isAdmin() ? '추가·삭제 후 저장하세요. 삭제하면 뒤 교시가 앞으로 이동합니다. 공강은 ‘공강’으로 입력하세요.' : '학생은 시간표를 확인할 수 있습니다. 수정이 필요하면 관리자에게 알려 주세요.';
  $('#school-guide').textContent = isAdmin() ? '아래에서 학교 이름만 입력해 검색할 수 있습니다.' : '학교 이름과 주소는 관리자가 등록한 정보입니다.';
  return true;
}
function go(page, reloadData=true) {
  if (!auth?.user) return;
  if (page === 'students' && !isAdmin()) return;
  $$('.page').forEach(p => p.hidden = p.id !== page);
  $$('#navigation button').forEach(b => b.classList.toggle('active', b.dataset.page === page));
  $('#title').textContent = ({home:'오늘 한눈에', meals:'급식 · 내 알레르기', timetable:'시간표 · 준비물', exams:'내 시험 · 공부 계획', settings:'학교 정보', students:'학생 가입 승인'})[page];
  notice('');
  if (page === 'home') loadMeals(today(), '#home-meal');
  if (reloadData && (page === 'home' || page === 'timetable')) run(refresh);
  if (page === 'students') run(loadStudents);
}
async function refresh(timetableOnly=false, requestedClass=null) {
  const sequence = ++refreshSequence, requestedWeek = viewWeek;
  const selection = requestedClass || data?.selection;
  const query = new URLSearchParams({week: String(requestedWeek)});
  if (selection) {
    query.set('grade', String(selection.grade));
    query.set('classroom', String(selection.classroom));
  }
  const nextData = await api('/api/data?' + query);
  query.set('week', '1');
  const nextHomeData = requestedWeek === 1 ? nextData : await api('/api/data?' + query);
  if (sequence !== refreshSequence) return;
  data = nextData; homeData = nextHomeData;
  if (timetableOnly) renderTimetableViews();
  else render();
}
function dDay(target) {
  const n = Math.round((Date.parse(target+'T00:00:00Z') - Date.parse(today()+'T00:00:00Z'))/86400000);
  return n > 0 ? `D-${n}` : n === 0 ? 'D-day' : `D+${-n} (종료)`;
}
function lessonHTML(lessons) {
  return lessons.length ? lessons.map((r,i) => `<div class="lesson"><span class="period">${i+1}교시</span><div><strong>${esc(r.subject)}</strong><small>준비물 · ${esc(r.supplies || '없음')}</small></div></div>`).join('') : '<p class="empty">등록된 수업이 없습니다.</p>';
}
function renderTimetableViews() {
  $('#view-grade').value = String(data.selection.grade);
  $('#view-class').value = data.selection.classroom;
  $('#view-week').value = String(viewWeek);
  const meta = data.timetable_meta;
  const status = data.timetable_live ? '컴시간에서 자동으로 불러온 시간표' : data.timetable_saved ? '관리자가 저장한 시간표' : (meta.error || '관리자가 컴시간 학교를 연결해야 합니다.');
  $('#class-summary').textContent = `${data.selection.grade}학년 ${data.selection.classroom}반 · ${data.week_start} 시작 주 · ${status}` + (meta.upstream_updated ? ` · 컴시간 수정: ${meta.upstream_updated}` : '');
  $('#class-summary').classList.toggle('unpublished', !data.timetable_available);
  $('#home-lessons').innerHTML = lessonHTML(homeData.timetable[String((new Date().getDay()+6)%7)] || []);
  renderLessons();
}
function render() {
  $('#greeting').textContent = `${auth.user.name}님, 오늘도 차근차근 시작해요.`;
  $('#school-heading').textContent = data.school.name || '관리자가 학교를 등록하면 급식을 확인할 수 있어요.';
  $('#current-school').replaceChildren();
  const name = document.createElement('h3'); name.textContent = data.school.name || '등록된 학교가 없습니다.';
  const address = document.createElement('p'); address.textContent = data.school.address || '';
  $('#current-school').append(name, address);
  $('#allergy-text').value = data.allergy_text;
  if (!$('#comcigan-school-name').value) $('#comcigan-school-name').value = data.comcigan_school.name || (data.school.name || '').replace(/고등학교$/, '고').replace(/중학교$/, '중');

  $('#allergy-guide').textContent = data.unknown_allergies.length ? '저장은 했지만 자동 경고에 연결되지 않은 식품: '+data.unknown_allergies.join(', ')+'. 학교의 원재료 안내에서 직접 확인하세요.' : '계란·달걀·난류, 소고기·쇠고기 등의 이름을 인식합니다. 쉼표로 구분해 주세요.';

  const exams = data.exams.filter(e => e.date >= today()).sort((a,b) => a.date.localeCompare(b.date));
  $('#home-exams').innerHTML = exams.length ? exams.map(e => `<div class="exam-summary"><span class="dday">${dDay(e.date)}</span><div><strong>${esc(e.name)}</strong><small>${esc(e.date)} · 공부 완료 ${e.ranges.filter(r=>r.done).length}/${e.ranges.length}</small></div></div>`).join('') : '<p class="empty">다가오는 시험이 없습니다.</p>';
  renderTimetableViews(); renderExams();
}
async function loadMeals(target, selector) {
  mealJobs.get(selector)?.abort();
  const controller = new AbortController();
  mealJobs.set(selector, controller);
  const timer = setTimeout(() => controller.abort('timeout'), 3000);
  const node = $(selector); node.textContent = '급식 정보를 불러오는 중…';
  try {
    const result = await api('/api/meals?date='+encodeURIComponent(target), 'GET', undefined, controller.signal);
    node.replaceChildren();
    if (result.message) { node.textContent = result.message; return; }
    for (const meal of result.meals) {
      const heading = document.createElement('h3'); heading.textContent = meal.type; node.append(heading);
      for (const item of meal.items) {
        const row = document.createElement('div'); row.className = 'meal-item'+(item.warnings.length ? ' warning' : '');
        row.textContent = (item.warnings.length ? '⚠ ' : '')+item.name;
        if (item.warnings.length) { const small = document.createElement('small'); small.textContent = '알레르기 주의: '+item.warnings.join(', '); row.append(small); }
        node.append(row);
      }
    }
  } catch (error) {
    if (mealJobs.get(selector) !== controller) return;
    node.textContent = controller.signal.aborted ? '조회가 지연되거나 취소되었습니다. 다시 조회해 주세요.' : error.message;
  } finally { clearTimeout(timer); }
}
function renumber() { $$('#lesson-editor .edit-row').forEach((row,i) => row.querySelector('span').textContent=`${i+1}교시`); }
function addLesson(subject='', supplies='') {
  if (!isAdmin()) return;
  if ($('#lesson-editor').children.length >= 12) { notice('최대 12교시까지 등록할 수 있습니다.',true); return; }
  const row = document.createElement('div'); row.className='edit-row';
  row.innerHTML=`<span></span><input aria-label="과목" name="subject" placeholder="과목" required maxlength="80" value="${esc(subject)}"><input aria-label="준비물" name="supplies" placeholder="준비물 (선택)" maxlength="200" value="${esc(supplies)}"><button type="button" class="danger">삭제</button>`;
  row.querySelector('button').onclick=()=>{row.remove();renumber();};
  $('#lesson-editor').append(row); renumber();
}
function renderLessons() {
  const lessons=data.timetable[$('#weekday').value] || [];
  $('#lesson-editor').replaceChildren();
  if (isAdmin()) lessons.forEach(r => addLesson(r.subject,r.supplies));
  else if (!data.timetable_available) {
    const selection = data.selection;
    $('#lesson-editor').innerHTML = `<p class="empty">${selection.grade}학년 ${selection.classroom}반 시간표를 가져오지 못했습니다. ${esc(data.timetable_meta.error || '관리자에게 컴시간 학교 연결을 요청하세요.')}</p>`;
  } else $('#lesson-editor').innerHTML=lessonHTML(lessons);
}
function renderExams() {
  $('#exam-list').innerHTML=data.exams.map((e,i)=>`<article class="card"><div class="exam-title"><div><h2>${esc(e.name)}</h2><span class="muted">${esc(e.date)} · ${dDay(e.date)}</span></div><div><button type="button" data-edit-exam="${i}">수정</button> <button type="button" class="danger" data-delete-exam="${i}">삭제</button></div></div><progress max="${Math.max(1,e.ranges.length)}" value="${e.ranges.filter(r=>r.done).length}" aria-label="공부 완료 진행률"></progress>${e.ranges.map((r,j)=>`<div class="range ${r.done?'done':''}"><input id="range-${i}-${j}" type="checkbox" data-check="${i},${j}" ${r.done?'checked':''}><label for="range-${i}-${j}">${esc(r.subject)} · ${esc(r.scope)}</label><button type="button" class="danger" data-delete-range="${i},${j}">삭제</button></div>`).join('')}<form class="inline range-form" data-exam="${i}"><label>과목<input name="subject" required maxlength="80" placeholder="수학"></label><label>공부 범위<input name="scope" required maxlength="300" placeholder="3단원 문제 풀기"></label><button type="submit">범위 추가</button></form></article>`).join('') || '<p class="empty">시험 일정을 등록해 보세요.</p>';
}
async function saveExams(change) {
  const exams=structuredClone(data.exams); change(exams);
  await api('/api/exams','POST',{exams}); await refresh(); notice('내 시험 계획을 저장했습니다.');
}
async function loadStudents() {
  const result=await api('/api/admin/students');
  studentRows = result.students;
  const names={pending:'승인 대기', approved:'승인됨', rejected:'거절 · 이용 제한'};
  $('#student-list').innerHTML=result.students.length ? `<div class="student-table"><table><thead><tr><th>학번</th><th>이름</th><th>상태</th><th>관리</th></tr></thead><tbody>${result.students.map(s=>`<tr><td>${esc(s.student_id)}</td><td>${esc(s.name)}</td><td><span class="status ${s.status}">${names[s.status]}</span></td><td><button type="button" data-student="${s.id}" data-status="approved">승인</button> <button type="button" data-student="${s.id}" data-status="rejected" class="danger">거절 · 승인 취소</button> <button type="button" data-student="${s.id}" data-status="pending">대기로 변경</button> <button type="button" class="danger" data-delete-student="${s.id}">학생 삭제</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="empty">아직 가입 신청이 없습니다.</p>';
}

$('#login-form').onsubmit=e=>{ e.preventDefault(); run(async()=>{
  await api('/api/login/'+(ADMIN_PAGE?'admin':'student'),'POST',{login:$('#login-id').value.trim(),password:$('#login-password').value});
  $('#login-password').value=''; await start(); notice('로그인했습니다.');
},true); };
$('#register-form').onsubmit=e=>{e.preventDefault();run(async()=>{
  if ($('#register-password').value!==$('#register-confirm').value) throw Error('비밀번호가 일치하지 않습니다.');
  const id=$('#register-id').value.trim();
  const result=await api('/api/register','POST',{student_id:id,name:$('#register-name').value.trim(),password:$('#register-password').value});
  e.target.reset(); $('#login-id').value=id; notice(result.message);
},true);};
$('#logout').onclick=()=>run(async()=>{await api('/api/logout','POST',{});location.assign(ADMIN_PAGE?'/admin':'/');},true);
$$('#navigation button').forEach(b=>b.onclick=()=>go(b.dataset.page));
$('#allergy-form').onsubmit=e=>{e.preventDefault();run(async()=>{await api('/api/allergies','POST',{text:$('#allergy-text').value});await refresh();notice('내 알레르기를 저장했습니다.');await Promise.all([loadMeals(today(),'#home-meal'),loadMeals($('#meal-date').value,'#meal-result')]);},true);};
$('#meal-form').onsubmit=e=>{e.preventDefault();loadMeals($('#meal-date').value,'#meal-result');};
$('#weekday').innerHTML=DAYS.map((d,i)=>`<option value="${i}">${d}요일</option>`).join('');
$('#weekday').value=String((new Date().getDay()+6)%7); $('#weekday').onchange=()=>{if(data)renderLessons();};
$('#add-lesson').onclick=()=>addLesson();
$('#clear-lessons').onclick=()=>{if(confirm('이 요일의 시간표를 비울까요? 저장하면 적용됩니다.'))$('#lesson-editor').replaceChildren();};
$('#timetable-form').onsubmit=e=>{e.preventDefault();run(async()=>{
  const lessons=$$('#lesson-editor .edit-row').map(r=>({subject:r.querySelector('[name=subject]').value.trim(),supplies:r.querySelector('[name=supplies]').value.trim()}));
  await api('/api/admin/timetable','POST',{day:$('#weekday').value,lessons,week:viewWeek});clearImportPreview();await refresh();notice('선택한 학급·주의 시간표를 저장했습니다.');
},true);};
$('#school-form').onsubmit=e=>{e.preventDefault();run(async()=>{
  const result=await api('/api/admin/schools?name='+encodeURIComponent($('#school-search').value.trim()));schoolResults=result.schools;
  $('#school-search-message').textContent=result.message || '';
  $('#school-results').innerHTML=schoolResults.length ? schoolResults.map((s,i)=>`<div class="school-result"><div><strong>${esc(s.name)}</strong><p class="muted">${esc(s.address)}</p></div><button type="button" data-school="${i}">이 학교 선택</button></div>`).join('') : '<p class="empty">검색 결과가 없습니다. 학교의 정식 이름으로 검색해 보세요.</p>';
});};
$('#school-results').onclick=e=>{const b=e.target.closest('[data-school]');if(!b)return;run(async()=>{
  const school=schoolResults[Number(b.dataset.school)];await api('/api/admin/school','POST',{office:school.office,code:school.code});await refresh();notice('학교 이름과 주소를 저장했습니다.');
},true);};
function clearImportPreview() {
  importTicket = null;
  $('#comcigan-preview').hidden = true;
}
function applyClassSelection() {
  clearTimeout(classChangeTimer);
  if (!auth?.user || !data) return;
  const grade = Number($('#view-grade').value);
  const classroom = Number($('#view-class').value);
  if (!Number.isInteger(grade) || grade < 1 || grade > 3 ||
      !Number.isInteger(classroom) || classroom < 1 || classroom > 40) return;
  if (data.selection.grade === grade && data.selection.classroom === classroom) return;
  if (mutationBusy) {
    classChangeTimer = setTimeout(applyClassSelection, 150);
    return;
  }
  run(async () => {
    ++refreshSequence; // 이전 학급의 늦은 응답이 새 선택을 덮어쓰지 않도록 한다.
    clearImportPreview();
    const loading = `${grade}학년 ${classroom}반 시간표를 불러오는 중…`;
    $('#class-summary').textContent = loading;
    $('#home-lessons').textContent = loading;
    $('#lesson-editor').textContent = loading;
    try {
      await api('/api/class', 'POST', {grade, classroom});
      await refresh(true, {grade, classroom});
      notice(data.timetable_available ? `${grade}학년 ${classroom}반 시간표로 바꿨습니다.` : (data.timetable_meta.error || '이 학급 시간표를 가져오지 못했습니다.'), !data.timetable_available);
    } catch (error) {
      renderTimetableViews();
      throw error;
    }
  }, true);
}
$('#view-grade').onchange = applyClassSelection;
$('#view-class').oninput = () => {
  clearTimeout(classChangeTimer);
  classChangeTimer = setTimeout(applyClassSelection, 400);
};
$('#view-class').onchange = applyClassSelection;
$('#class-form').onsubmit = e => {
  e.preventDefault();
  applyClassSelection();
};
$('#view-week').onchange = () => run(async () => {
  viewWeek = Number($('#view-week').value); clearImportPreview(); await refresh(true);
}, true);
$('#comcigan-search-form').onsubmit = e => {
  e.preventDefault();
  run(async () => {
    clearImportPreview();
    $('#comcigan-schools').textContent = '컴시간에서 학교를 찾는 중…';
    try {
      const result = await api('/api/admin/comcigan/schools?name=' + encodeURIComponent($('#comcigan-school-name').value.trim()));
      comciganSchools = result.schools;
      $('#comcigan-schools').innerHTML = comciganSchools.length ? comciganSchools.map((school,i) => `<div class="school-result"><div><strong>${esc(school.name)}</strong><p class="muted">${esc(school.region)}</p></div><button type="button" data-comcigan-school="${i}">이 학교 시간표 불러오기</button></div>`).join('') : '<p class="empty">검색 결과가 없습니다. 컴시간알리미에 표시된 학교 이름으로 검색하세요.</p>';
    } catch (error) { $('#comcigan-schools').textContent = ''; throw error; }
  }, true);
};
$('#comcigan-schools').onclick = e => {
  const button = e.target.closest('[data-comcigan-school]');
  if (!button) return;
  run(async () => {
    clearImportPreview(); notice('선택한 학급의 시간표를 불러오는 중…');
    const school = comciganSchools[Number(button.dataset.comciganSchool)];
    const result = await api('/api/admin/comcigan/preview', 'POST', {name:school.name, code:school.code, week:viewWeek});
    const preview = result.preview;
    importTicket = result.ticket;
    $('#comcigan-preview-title').textContent = `${preview.school.name} · ${preview.grade}학년 ${preview.classroom}반 (미리보기 · 아직 학생에게 미공개)`;
    $('#comcigan-preview-info').textContent = `${preview.week_start} ~ ${preview.week_end} · 컴시간 수정: ${preview.upstream_updated || '확인 불가'}` + (preview.restricted_days.length ? ' · 열람 제한된 요일은 비워 두었습니다.' : '');
    const periods = Math.max(...Object.values(preview.days).map(day => day.length));
    $('#comcigan-preview-table').innerHTML = `<table><thead><tr><th>교시</th>${DAYS.slice(0,5).map(day=>`<th>${day}</th>`).join('')}</tr></thead><tbody>${Array.from({length:periods},(_,i)=>`<tr><th>${i+1}</th>${DAYS.slice(0,5).map((_,day)=>`<td>${esc(preview.days[String(day)]?.[i]?.subject || '—')}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
    $('#comcigan-preview').hidden = false;
    notice('아직 학생에게 보이지 않습니다. 내용을 확인하고 ‘이 시간표 저장 · 학생에게 공개’를 눌러 주세요.');
  }, true);
};
$('#import-comcigan').onclick = () => run(async () => {
  if (!importTicket) throw Error('시간표를 먼저 불러오세요.');
  const result = await api('/api/admin/comcigan/import', 'POST', {ticket:importTicket});
  clearImportPreview(); await refresh(); notice(result.message);
}, true);
$('#refresh-timetable').onclick=()=>run(async()=>{await refresh();notice(data.timetable_available ? '선택한 학급의 시간표를 새로 불러왔습니다.' : (data.timetable_meta.error || '이 학급의 시간표를 가져오지 못했습니다.'), !data.timetable_available);});
$('#refresh-students').onclick=()=>run(loadStudents);
$('#student-list').onclick = e => {
  if (mutationBusy) return;
  const removeButton = e.target.closest('[data-delete-student]');
  if (removeButton) {
    const student = studentRows.find(row => row.id === Number(removeButton.dataset.deleteStudent));
    if (!student) { notice('학생 목록을 새로고침해 주세요.', true); return; }
    const confirmed = confirm(`${student.student_id} ${student.name} 학생 계정을 삭제할까요?\n\n로그인이 즉시 해제되며, 개인 알레르기와 시험 계획도 함께 삭제됩니다. 삭제 후에는 복구할 수 없습니다.`);
    if (!confirmed) return;
    run(async () => {
      const result = await api('/api/admin/delete-student', 'POST', {id:student.id, student_id:student.student_id});
      await loadStudents();
      notice(result.message);
    }, true);
    return;
  }
  const button = e.target.closest('[data-student]');
  if (!button) return;
  run(async () => {
    await api('/api/admin/approve', 'POST', {id:Number(button.dataset.student), status:button.dataset.status});
    await loadStudents();
    notice('학생 승인 상태를 변경했습니다.');
  }, true);
};
$('#exam-form').onsubmit=e=>{e.preventDefault();run(async()=>{await saveExams(exams=>exams.push({name:$('#exam-name').value.trim(),date:$('#exam-date').value,ranges:[]}));e.target.reset();},true);};
$('#exam-list').onsubmit=e=>{if(!e.target.matches('.range-form'))return;e.preventDefault();const form=e.target;run(()=>saveExams(exams=>exams[Number(form.dataset.exam)].ranges.push({subject:form.elements.subject.value.trim(),scope:form.elements.scope.value.trim(),done:false})),true);};
$('#exam-list').onchange=e=>{if(!e.target.matches('[data-check]'))return;const [i,j]=e.target.dataset.check.split(',').map(Number),done=e.target.checked;run(async()=>{try{await saveExams(exams=>{exams[i].ranges[j].done=done;});}catch(error){renderExams();throw error;}},true);};
$('#exam-list').onclick=e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.hasAttribute('data-delete-exam')&&confirm('이 시험과 공부 범위를 삭제할까요?'))run(()=>saveExams(exams=>exams.splice(Number(b.dataset.deleteExam),1)),true);
  if(b.hasAttribute('data-delete-range')){const [i,j]=b.dataset.deleteRange.split(',').map(Number);run(()=>saveExams(exams=>exams[i].ranges.splice(j,1)),true);}
  if(b.hasAttribute('data-edit-exam')){const i=Number(b.dataset.editExam),name=prompt('시험 이름',data.exams[i].name);if(name===null)return;const target=prompt('시험 날짜 (YYYY-MM-DD)',data.exams[i].date);if(target===null)return;run(()=>saveExams(exams=>{exams[i].name=name.trim();exams[i].date=target;}),true);}
};
async function start(){
  $('#today').textContent=new Date().toLocaleDateString('ko-KR',{year:'numeric',month:'long',day:'numeric',weekday:'long'});
  if(!await showSession())return;
  $('#meal-date').value=today();data=null; homeData=null; await refresh();go(isAdmin()&&ADMIN_PAGE?'students':'home', false);
}
run(start);


// 화면상의 단축키 제한은 보안 경계가 아니다. 권한은 서버가 검사한다.
document.addEventListener('contextmenu', event => {
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);

document.addEventListener('keydown', event => {
  const key = (event.key || '').toLowerCase();
  const command = event.ctrlKey || event.metaKey;
  if (event.ctrlKey && event.shiftKey && !event.altKey && key === 'a') {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.repeat) return;
    if (auth?.user?.role === 'student') {
      notice('학생 계정으로 관리자 화면에 접근할 수 없습니다.', true);
      return;
    }
    location.assign('/admin');
    return;
  }
  const inspect = command && (event.shiftKey || event.altKey) && ['i','j','c'].includes(key);
  const source = command && key === 'u';
  const contextMenu = key === 'contextmenu' || (event.shiftKey && key === 'f10');
  if (key === 'f12' || inspect || source || contextMenu) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

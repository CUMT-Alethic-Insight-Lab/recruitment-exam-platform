const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {webcrypto} = require('node:crypto');
const html = fs.readFileSync(process.argv[2], 'utf8');
const elements = new Map();
for (const match of html.matchAll(/id="([^"]+)"/g)) {
  elements.set(match[1], {value: '', textContent: '', innerHTML: '', disabled: false, checked: false,
    classList: {add(){}, remove(){}, toggle(){}}, style: {}, addEventListener(){},
    removeAttribute(){}, focus(){}, querySelectorAll(){return []}});
}
const topics = [...html.matchAll(/name="meeting_topics" value="([^"]+)"/g)].map(m => ({value:m[1],checked:false,disabled:false}));
const document = {getElementById(id){assert(elements.has(id), `Missing DOM: ${id}`); return elements.get(id)},
  querySelectorAll(selector){return selector.includes('meeting_topics') ? topics : []},addEventListener(){}};
let profile = {};
let completed = false;
const identity = {name:'页面测试', student_id:'00009317',club_name:'Alethic Insight人工智能社团',club_position:''};
const storage = new Map([['alethic_session','temporary-token']]);
const context = vm.createContext({document, console, TextEncoder, Uint8Array, crypto:webcrypto,
  localStorage:{getItem(k){return storage.get(k)||null},setItem(k,v){storage.set(k,v)},removeItem(k){storage.delete(k)}},
  location:{replace(){throw Error('Unexpected redirect')}},setInterval(){return 1},clearInterval(){},URL,
  async fetch(url, options={}) {
    if(url.endsWith('/api/auth/me')) return {ok:true,status:200,json:async()=>({role:'member',pw_changed:true,student_id:identity.student_id,name:identity.name,member_identity:identity,profile,profile_completed:completed,profile_errors:{}})};
    assert(url.includes('/api/auth/profile'));
    const patch = JSON.parse(options.body).profile;
    profile={...profile,...patch};
    if(url.endsWith('/complete') && !profile.email && !profile.school_email) return {ok:false,status:400,json:async()=>({detail:{fields:{email:'至少填写一个邮箱'}}})};
    completed=url.endsWith('/complete');
    return {ok:true,status:200,json:async()=>({profile,profile_completed:completed,profile_errors:completed?{}:{email:'至少填写一个邮箱'}})};
  }});
(async()=>{
  assert(!html.includes('id="nickname"'));
  assert(!html.includes('saveNickname'));
  const scripts=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  vm.runInContext(scripts.at(-1)[1],context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(elements.get('profile-name').textContent,'页面测试');
  assert.equal(elements.get('member-complete-btn').disabled,false);
  elements.get('member-phone').value='13800138000';
  topics[1].checked=true;
  elements.get('member-meeting-other').value='独立回答';
  await vm.runInContext('saveMemberProfile(false)',context);
  assert(elements.get('member-msg').textContent.includes('草稿已保存'));
  assert.equal(completed,false);
  assert.deepEqual(profile.meeting_topics,['resources']);
  await vm.runInContext('saveMemberProfile(true)',context);
  assert(elements.get('member-msg').textContent.includes('至少填写一个邮箱'));
  elements.get('member-email').value='test@example.com';
  await vm.runInContext('saveMemberProfile(true)',context);
  assert(elements.get('member-msg').textContent.includes('后端核验通过'));
  await vm.runInContext('loadMe()',context);
  assert.equal(elements.get('member-meeting-other').value,'独立回答');
  assert.equal(topics[1].checked,true);
  vm.runInContext('setForcedPwMode(true)',context);
  assert(elements.get('member-save-btn').disabled);
  assert(elements.get('member-complete-btn').disabled);
  console.log('PAGE JS PASS: no stale nickname DOM; name; draft/complete distinction; error rendering; meeting reload; force-password gate');
})().catch(error=>{console.error(error);process.exit(1)});

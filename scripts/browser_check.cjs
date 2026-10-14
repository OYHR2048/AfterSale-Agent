// Headless test of our own local demo only; never print development credentials.
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
async function main(){
 const runtime=path.resolve(process.env.AFTERSALE_RUNTIME_DIR);
 const auth=JSON.parse(fs.readFileSync(path.join(runtime,'auth.json'),'utf8'));
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1360,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8018');
  await page.locator('#token').fill(auth.alice);
  await page.locator('#login').click();
  await page.locator('#identity').filter({hasText:'alice'}).waitFor();
  await page.locator('#message').fill('ORD10002 定制杯质量问题，照片显示破损，优先换货');
  await page.locator('#new').click();
  await page.locator('#status').filter({hasText:'等待确认'}).waitFor();
  const before=await page.locator('#proposal').innerText();
  if(!before.includes('换货')||!before.includes('平台'))throw Error('Wrong proposal');
  const taskId=await page.locator('#taskid').innerText();
  const noWrite=await page.evaluate(async id=>(await (await fetch('/api/tasks/'+id+'/records')).json()).length,taskId);
  if(noWrite!==0)throw Error('Unexpected unconfirmed business write');
  await page.screenshot({path:path.join(runtime,'waiting-confirmation.png'),fullPage:true});
  await page.locator('#confirm').click();
  await page.locator('#status').filter({hasText:'完成'}).waitFor();
  const result=await page.locator('#result').innerText();
  if(!result.includes('requested')||!result.includes('不代表'))throw Error('Business result missing notice');
  await page.reload();
  await page.locator('#reload').click();
  await page.locator('#tasks button').first().click();
  await page.locator('#status').filter({hasText:'完成'}).waitFor();
  await page.screenshot({path:path.join(runtime,'completed.png'),fullPage:true});
  const report={browser:'headless Microsoft Edge',proposal_checked:true,unconfirmed_record_count:noWrite,completed:true,refresh_restored:true,events_visible:await page.locator('#timeline .event').count(),javascript_errors:errors};
  fs.writeFileSync(path.join(runtime,'browser-check.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
  if(errors.length)throw Error('Browser JS errors');
 } finally {await browser.close()}
}
main().catch(e=>{console.error(e.message);process.exitCode=1});

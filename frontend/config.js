/* 拆分部署时的后端地址 —— 只对线上站点生效。
   · 本地开发 (python run.py, localhost:8000) 永远走同源后端，本文件被忽略；
   · GitHub Pages 部署后把它改成你的后端地址，例如：
       window.API_BASE = "https://travel-agent-xxxx.onrender.com";
   · 不改文件也可：访问 https://<页地址>/?api=<后端地址> 一次即写入浏览器；
     ?api=local 可清除该覆盖。 */
window.API_BASE = "https://travel-agent-c4dk.onrender.com";

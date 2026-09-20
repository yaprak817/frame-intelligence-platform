"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import styles from "./brands.module.css";

type Brand={id:string;name:string;status:string;class_count:number;dataset_count:number;updated_at:string};

async function api<T>(path:string,init?:RequestInit):Promise<T>{
  const response=await fetch(`/api/v1${path}`,{...init,headers:{"Content-Type":"application/json",...(init?.headers??{})}});
  if(!response.ok){throw new Error("İşlem tamamlanamadı.");}
  return response.json() as Promise<T>;
}

export function BrandsClient(){
  const [brands,setBrands]=useState<Brand[]>([]);const [name,setName]=useState("");const [query,setQuery]=useState("");const [busy,setBusy]=useState(false);const [error,setError]=useState<string|null>(null);
  const pending=useRef(false);const mounted=useRef(false);
  useEffect(()=>{mounted.current=true;const controller=new AbortController();api<Brand[]>("/brands?limit=100&offset=0",{signal:controller.signal}).then((items)=>{if(mounted.current)setBrands(items);}).catch(()=>{if(mounted.current&&!controller.signal.aborted)setError("Markalar yüklenemedi.");});return()=>{mounted.current=false;controller.abort();};},[]);
  const filtered=useMemo(()=>{const q=query.trim().toLocaleLowerCase("tr-TR");return q?brands.filter((b)=>b.name.toLocaleLowerCase("tr-TR").includes(q)):brands;},[brands,query]);
  async function create(e:FormEvent){e.preventDefault();if(!name.trim()||pending.current)return;pending.current=true;setBusy(true);setError(null);try{const created=await api<Brand>("/brands",{method:"POST",body:JSON.stringify({name})});if(mounted.current){setBrands((v)=>[created,...v]);setName("");}}catch{if(mounted.current)setError("Marka oluşturulamadı.");}finally{pending.current=false;if(mounted.current)setBusy(false);}}
  return <main className={styles.shell}>
    <div className={styles.top}><div><h1 className={styles.title}>Markalarım</h1><p className={styles.sub}>Markaları, sınıfları ve veri setlerini tek yerden yönetin.</p></div><form className={styles.form} onSubmit={create}><input className={styles.input} value={name} onChange={(e)=>setName(e.target.value)} placeholder="Yeni marka adı"/><button className={styles.button} disabled={busy||!name.trim()}>+ Marka oluştur</button></form></div>
    {error&&<div className={styles.error} role="alert">{error}</div>}
    <section className={styles.panel}><div className={styles.toolbar}><strong>Markalar</strong><input className={styles.input} value={query} onChange={(e)=>setQuery(e.target.value)} placeholder="Ara"/></div><div className={styles.head}><span>Detaylar</span><span>Durum</span><span>Sınıflar</span><span>Veri setleri</span><span>Güncelleme</span></div>
    {filtered.length===0?<div className={styles.empty}>Henüz marka yok. İlk markanızı oluşturun.</div>:filtered.map((b)=><div className={styles.row} key={b.id}><Link className={styles.link} href={`/brands/${b.id}`}>{b.name}</Link><span className={styles.status}>● {b.status==="READY"?"Ready":"Draft"}</span><strong>{b.class_count}</strong><strong>{b.dataset_count}</strong><span className={styles.muted}>{new Intl.DateTimeFormat("tr-TR",{dateStyle:"medium"}).format(new Date(b.updated_at))}</span></div>)}</section>
  </main>;
}

// SPDX-License-Identifier: GPL-3.0-only
// Frame-boundary scheduler: a new target never touches an in-flight waveform.
struct MidiEngine {
  bool enabled=false, valid=false, blackout=false, ramping=false;
  uint16_t slots[9]={}, lastSlots[9]={};
  bool lastSlotsValid=false;
  uint8_t rgb8[27]={};
  float from[27]={};
  uint32_t seq=0, emitted=0, coalesced=0, completed=0, failures=0;
  uint32_t guardUs=1000, fadeMs=100, bitUs=250;
  uint8_t burst=1, settle=2, repeats=0, phase=0;
  uint8_t triggers[8]={}, head=0, count=0, triggerRun=0;
  uint32_t triggersCompleted=0, droppedTriggers=0, aborted=0;
  uint32_t inFlightSeq=0, lastTxUs=0, maxTxUs=0;
  int64_t lastInputUs=0, nextUs=0, rampStartUs=0;
  String error;
} midiEngine;
bool midiPulseReady=false;

void renderMidi(int64_t now, float out[27]) {
  float progress=1.f;
  if (midiEngine.ramping && midiEngine.fadeMs)
    progress=constrain(float(now-midiEngine.rampStartUs)/(midiEngine.fadeMs*1000.f),0.f,1.f);
  for(int i=0;i<27;++i)
    out[i]=midiEngine.blackout ? 0.f : midiEngine.from[i]+(midiEngine.rgb8[i]-midiEngine.from[i])*progress;
}
bool isMidiCommand(const String& c) {
  return c=="ENGINE" || c=="SET_STATE" || c=="GET_ENGINE" || c=="TRIGGER" || c=="BLACKOUT";
}
void cancelMidiEngine(bool clearBlackout=false) {
  midiEngine.enabled=false; midiEngine.valid=false; midiEngine.ramping=false;
  if(clearBlackout) midiEngine.blackout=false;
  midiEngine.count=0; midiEngine.head=0; midiEngine.repeats=0; midiEngine.triggerRun=0;
}
void finishMidiJob(PulseTaskConfig* job) {
  xSemaphoreTake(commandMutex,portMAX_DELAY);
  if(job->engineJob) {
    midiEngine.nextUs=esp_timer_get_time()+midiEngine.guardUs;
    if(job->engineSuccess) {
      const uint32_t duration=uint32_t(esp_timer_get_time()-job->engineStartedUs);
      midiEngine.lastTxUs=duration; midiEngine.maxTxUs=max(midiEngine.maxTxUs,duration);
      if(job->engineTrigger) {
        ++midiEngine.triggersCompleted;
        midiEngine.lastSlotsValid=false; // Reassert target after the transient pulse.
      } else {
        memcpy(midiEngine.lastSlots,job->engineSlots,sizeof(midiEngine.lastSlots));
        midiEngine.lastSlotsValid=true;
        if(job->engineFinal) midiEngine.emitted=job->engineSeq;
        ++midiEngine.completed;
      }
    } else if(abortRequested) ++midiEngine.aborted;
    else {
      ++midiEngine.failures;
      midiEngine.error="RF transaction failed; engine disabled; inspect hardware";
      cancelMidiEngine();
    }
    midiEngine.inFlightSeq=0;
  }
  txBusy=false;
  xSemaphoreGive(commandMutex);
}
bool handleMidiCommand(const String& id,const String& command,JsonObject args) {
  if(!isMidiCommand(command)) return false;
  JsonDocument result;
  const int64_t now=esp_timer_get_time();
  if(command=="GET_ENGINE") {
    result["version"]="midi-stage2";
    result["enabled"]=midiEngine.enabled; result["valid"]=midiEngine.valid;
    result["blackout_latched"]=midiEngine.blackout;
    result["state_seq"]=midiEngine.seq; result["emit_seq"]=midiEngine.emitted;
    result["state_frames_completed"]=midiEngine.completed;
    result["triggers_completed"]=midiEngine.triggersCompleted;
    result["triggers_pending"]=midiEngine.count; result["dropped_triggers"]=midiEngine.droppedTriggers;
    result["coalesced"]=midiEngine.coalesced; result["tx_busy"]=txBusy;
    result["failures"]=midiEngine.failures; result["frames_aborted"]=midiEngine.aborted;
    result["last_error"]=midiEngine.error; result["burst"]=midiEngine.burst;
    result["guard_us"]=midiEngine.guardUs; result["bit_us"]=midiEngine.bitUs;
    result["fade_ms"]=midiEngine.fadeMs; result["settle_frames"]=midiEngine.settle;
    result["ramping"]=midiEngine.ramping; result["repeats_pending"]=midiEngine.repeats;
    result["last_tx_us"]=midiEngine.lastTxUs; result["max_tx_us"]=midiEngine.maxTxUs;
    result["policy"]="frame_boundary"; result["lease_ms"]=3000;
    result["frame_air_us"]=521*midiEngine.bitUs;
    result["precision"]=8; result["air_rgb_bits"]=4;
    JsonArray last=result["emitted_slots"].to<JsonArray>();
    JsonArray target=result["target_slots"].to<JsonArray>();
    for(int i=0;i<9;++i) { last.add(midiEngine.lastSlots[i]); target.add(midiEngine.slots[i]); }
    respondOk(id,result); return true;
  }
  if(command=="ENGINE") {
    if(!args["enabled"].is<bool>()) { respondError(id,"enabled boolean required"); return true; }
    const int burst=args["burst"] | int(midiEngine.burst);
    const int guard=args["guard_us"] | int(midiEngine.guardUs);
    const int fade=args["fade_ms"] | int(midiEngine.fadeMs);
    const int bit=args["bit_us"] | int(midiEngine.bitUs);
    const int settle=args["settle_frames"] | int(midiEngine.settle);
    const String policy=args["policy"] | "frame_boundary";
    if((burst!=1&&burst!=2&&burst!=3&&burst!=6)||guard<0||guard>100000||fade<0||fade>2000
        ||(bit!=200&&bit!=250)||settle<0||settle>6
        ||(policy!="frame_boundary"&&policy!="exact"&&policy!="auto")) {
      respondError(id,"burst 1/2/3/6, guard_us 0..100000, fade_ms 0..2000, bit_us 200/250, settle_frames 0..6 required");return true;
    }
    if(args["enabled"].as<bool>()) {
      if(!radioFound||recorder.snapshot().recording||(txBusy&&!midiEngine.enabled)) {
        respondError(id,"radio unavailable or owned by another operation");return true;
      }
      midiEngine.enabled=true; midiEngine.lastInputUs=now; midiEngine.error="";
      midiEngine.burst=burst; midiEngine.guardUs=guard; midiEngine.fadeMs=fade;
      midiEngine.bitUs=bit; midiEngine.settle=settle;
    } else {
      // Graceful stop. Only explicit ABORT may cut a waveform.
      cancelMidiEngine();
    }
  } else {
    if(!midiEngine.enabled) { respondError(id,"enable ENGINE first");return true; }
    if(command=="TRIGGER") {
      const String kind=args["kind"] | "pulse";
      if(kind!="pulse"||!args["palette"].is<int>()||args["palette"].as<int>()<0||args["palette"].as<int>()>15) {
        respondError(id,"only pulse with palette 0..15 supported");return true;
      }
      if(midiEngine.blackout) { respondError(id,"blackout latched");return true; }
      if(midiEngine.count==8) { ++midiEngine.droppedTriggers;respondError(id,"trigger queue full");return true; }
      midiEngine.triggers[(midiEngine.head+midiEngine.count)%8]=args["palette"].as<int>();++midiEngine.count;
    } else {
      uint16_t incoming[9]={}; uint8_t fine[27]={};
      if(command=="SET_STATE") {
        if(midiEngine.blackout&&!(args["release_blackout"] | false)) {
          respondError(id,"blackout latched; release_blackout=true required");return true;
        }
        JsonArray slots=args["slots"].as<JsonArray>();
        if(slots.isNull()||slots.size()!=9) { respondError(id,"exactly 9 slots required");return true; }
        for(int i=0;i<9;++i) {
          if(!slots[i].is<int>()||slots[i].as<int>()<0||slots[i].as<int>()>65535) {
            respondError(id,"slots must be integers 0..65535");return true;
          }
          incoming[i]=slots[i].as<int>();
          for(int c=0;c<3;++c) fine[i*3+c]=((incoming[i]>>(8-c*4))&15)*17;
        }
        if(!args["rgb8"].isNull()) {
          JsonArray rgb=args["rgb8"].as<JsonArray>();
          if(rgb.isNull()||rgb.size()!=27) { respondError(id,"rgb8 requires 27 integers 0..255");return true; }
          for(int i=0;i<27;++i) {
            if(!rgb[i].is<int>()||rgb[i].as<int>()<0||rgb[i].as<int>()>255) {
              respondError(id,"rgb8 requires 27 integers 0..255");return true;
            }
            fine[i]=rgb[i].as<int>();
          }
          for(int i=0;i<9;++i) {
            incoming[i]&=0xF000;
            for(int c=0;c<3;++c) incoming[i]|=uint16_t((fine[i*3+c]+8)/17)<<(8-c*4);
          }
        }
      }
      const bool changed=command=="BLACKOUT"||!midiEngine.valid
        ||memcmp(incoming,midiEngine.slots,sizeof(incoming))||memcmp(fine,midiEngine.rgb8,sizeof(fine));
      if(changed) {
        float previous[27];renderMidi(now,previous);
        if(!midiEngine.valid) {
          for(int i=0;i<9;++i) for(int c=0;c<3;++c)
            previous[i*3+c]=midiEngine.lastSlotsValid?((midiEngine.lastSlots[i]>>(8-c*4))&15)*17:0;
        }
        if(midiEngine.valid&&midiEngine.seq!=midiEngine.emitted&&midiEngine.seq!=midiEngine.inFlightSeq) ++midiEngine.coalesced;
        memcpy(midiEngine.from,previous,sizeof(previous));
        memcpy(midiEngine.slots,incoming,sizeof(incoming));memcpy(midiEngine.rgb8,fine,sizeof(fine));
        if(++midiEngine.seq==0) ++midiEngine.seq;
        midiEngine.valid=true; midiEngine.rampStartUs=now;
        midiEngine.ramping=midiEngine.fadeMs>0&&command!="BLACKOUT";
        midiEngine.repeats=midiEngine.burst+midiEngine.settle;
        midiEngine.phase=0;
      }
      if(command=="BLACKOUT") {
        midiEngine.blackout=true; midiEngine.ramping=false;
        midiEngine.count=0;midiEngine.head=0;
        // Discard pending actions, but finish the complete current RF frame.
      } else if(args["release_blackout"] | false) midiEngine.blackout=false;
    }
    midiEngine.lastInputUs=now;
  }
  result["accepted"]=true;result["state_seq"]=midiEngine.seq;result["enabled"]=midiEngine.enabled;
  respondOk(id,result);return true;
}
void serviceMidiEngine() {
  if(commandMutex==nullptr||xSemaphoreTake(commandMutex,0)!=pdTRUE) return;
  const int64_t now=esp_timer_get_time();
  if(midiEngine.enabled&&now-midiEngine.lastInputUs>3000000) {
    midiEngine.error="host lease expired; scheduling stopped (stick state may remain)";
    cancelMidiEngine(); // Stop scheduling, never truncate the current frame.
  }
  if(!midiEngine.enabled&&!txBusy&&midiPulseReady) {
    String error;cleanupPulseRmt(error);midiPulseReady=false;
    if(!error.isEmpty()) midiEngine.error=error;
  }
  if(!midiEngine.enabled||txBusy||now<midiEngine.nextUs||(!midiEngine.count&&!midiEngine.valid)) {
    xSemaphoreGive(commandMutex);return;
  }
  float rendered[27];renderMidi(now,rendered);
  if(midiEngine.ramping&&now-midiEngine.rampStartUs>=midiEngine.fadeMs*1000LL) midiEngine.ramping=false;
  uint16_t snapshot[9]={};
  for(int i=0;i<9;++i) {
    snapshot[i]=midiEngine.slots[i]&0xF000;
    for(int c=0;c<3;++c) snapshot[i]|=uint16_t(constrain(int(rendered[i*3+c]/17.f+0.5f),0,15))<<(8-c*4);
  }
  const bool same=midiEngine.lastSlotsValid&&!memcmp(snapshot,midiEngine.lastSlots,sizeof(snapshot));
  const bool final=!midiEngine.ramping;
  const bool atTarget=!memcmp(snapshot,midiEngine.slots,sizeof(snapshot));
  const bool quiet=now-midiEngine.rampStartUs>=150000;
  if(!midiEngine.count&&same&&(!final||!midiEngine.repeats)) {
    if(final) midiEngine.emitted=midiEngine.seq;
    xSemaphoreGive(commandMutex);return;
  }
  if(!midiEngine.count&&same&&final&&!quiet) {
    midiEngine.emitted=midiEngine.seq;
    xSemaphoreGive(commandMutex);return;
  }
  auto* job=new(std::nothrow) PulseTaskConfig();
  if(!job) {
    midiEngine.error="allocation failed";++midiEngine.failures;cancelMidiEngine();
    xSemaphoreGive(commandMutex);return;
  }
  job->engineJob=true;job->engineSeq=midiEngine.seq;job->engineFinal=atTarget;
  memcpy(job->engineSlots,snapshot,sizeof(snapshot));
  midiEngine.inFlightSeq=midiEngine.seq;
  job->frequencyHz=433920000;job->powerDbm=-30;job->repeat=1;job->gapUs=20000;job->startLevel=1;
  if(midiEngine.count&&(midiEngine.triggerRun<2||!midiEngine.valid)) {
    job->engineTrigger=true;
    uint8_t frame[3]={0xA6,midiEngine.triggers[midiEngine.head],0};
    frame[2]=uint8_t(0x96+frame[0]+frame[1]);
    appendFrameDurations(frame,3,midiEngine.bitUs,job->durations);
    ++midiEngine.triggerRun;
  } else {
    midiEngine.triggerRun=0;
    uint8_t frame[D8_FRAME_BYTES];
    buildD8Frame(snapshot,D8_PHASE_SEQUENCE[midiEngine.phase%6],frame);
    appendD8Durations(frame,midiEngine.bitUs,job->durations);
    // Redundancy is scheduled frame by frame; a newer state wins at the next boundary.
    midiEngine.phase=(midiEngine.phase+1)%6;
    if(atTarget&&midiEngine.repeats) --midiEngine.repeats;
  }
  abortRequested=false;txBusy=true;
  const bool trigger=job->engineTrigger;
  if(xQueueSend(pulseQueue,&job,0)!=pdTRUE) {
    txBusy=false;delete job;
    midiEngine.error="pulse worker queue unavailable";++midiEngine.failures;cancelMidiEngine();
  } else if(trigger) { midiEngine.head=(midiEngine.head+1)%8;--midiEngine.count; }
  xSemaphoreGive(commandMutex);
}

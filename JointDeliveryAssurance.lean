import Std

/-!
# Joint Delivery Participation Assurance
Copyright © 2026 AI Assurance, Inc. All rights reserved.
利用条件は README.md を参照。

公開用の限定モデル。入力から抽出した観測値を用いて8条件、参加、
引渡し、単便採算、状態を保持した模擬操作を定義する。
SHA-256、本人認証、観測の真実性、実配送、ソフトウェア全体は証明対象外。
Pythonの結論をこのモデルの入力には使わない。poc.py lean-test が
生の数量・時刻・確認値と、識別情報の一致という観測境界を射影する。
-/

set_option autoImplicit false
set_option warningAsError true

namespace JointDeliveryAssurance

inductive Status where
  | PASS | HOLD | REJECT
  deriving DecidableEq, Repr

inductive BusinessStatus where
  | POSITIVE | NEGATIVE | BREAKEVEN | UNKNOWN
  deriving DecidableEq, Repr

def profileSHA256 : String :=
  "7db4db5624c5475d1d2c80ee731ba5bffbc3cb90deeb12fa57ff98b1c1b7062f"

def worst (xs : List Status) : Status :=
  if Status.REJECT ∈ xs then .REJECT
  else if Status.HOLD ∈ xs then .HOLD else .PASS

def checked (v : Bool) : Status := if v then .PASS else .REJECT

def required (v : Option Bool) : Status :=
  match v with
  | none => .HOLD
  | some false => .REJECT
  | some true => .PASS

def sameNat (v : Option Nat) (expected : Nat) : Status :=
  match v with
  | none => .HOLD
  | some n => checked (decide (n = expected))

def knownSum (xs : List (Option Nat)) : Nat :=
  (xs.map (fun v => v.getD 0)).sum

def resourceStatus (xs : List (Option Nat)) (limit : Nat) : Status :=
  if knownSum xs ≤ limit then
    if none ∈ xs then .HOLD else .PASS
  else .REJECT

def timeStatus (now begins ends : Nat) : Status :=
  if begins ≤ now ∧ now < ends then .PASS else .HOLD

structure Binding where
  targetMatches : Bool
  revision : Nat
  expectedRevision : Nat
  hashMatches : Bool
  deriving DecidableEq, Repr

def bindingStatus (b : Binding) : Status :=
  worst [checked b.targetMatches,
    if b.revision < b.expectedRevision then .HOLD
    else if b.expectedRevision < b.revision then .REJECT
    else checked b.hashMatches]

structure Approval where
  binding : Binding
  actorMatches : Bool
  active : Bool
  issued : Nat
  expires : Nat
  deriving DecidableEq, Repr

def approvalStatus (now : Nat) (a : Option Approval) : Status :=
  match a with
  | none => .HOLD
  | some v => worst [bindingStatus v.binding, checked v.actorMatches,
      if v.active then .PASS else .HOLD, timeStatus now v.issued v.expires]

structure Evidence where
  binding : Binding
  observed : Nat
  expires : Nat
  capacity : Option Bool
  schedule : Option Bool
  deriving DecidableEq, Repr

def evidenceStatus (now : Nat) (e : Option Evidence) : Status :=
  match e with
  | none => .HOLD
  | some v => worst [bindingStatus v.binding, timeStatus now v.observed v.expires,
      required v.capacity, required v.schedule]

structure Delivery where
  arrival : Option Nat
  windowStart : Nat
  windowEnd : Nat
  latestExisting : Option Nat
  deriving DecidableEq, Repr

def deliveryStatus (d : Delivery) : Status :=
  match d.arrival with
  | none => .HOLD
  | some n => worst [checked (decide (d.windowStart ≤ n ∧ n ≤ d.windowEnd)),
      match d.latestExisting with
      | none => .PASS
      | some limit => checked (decide (n ≤ limit))]

def orderStatus (a b : Option Nat) : Status :=
  match a, b with
  | some x, some y => checked (decide (x ≤ y))
  | _, _ => .PASS

structure Handoff where
  binding : Binding
  observed : Nat
  expires : Nat
  partiesMatch : Bool
  quantity : Option Bool
  condition : Option Bool
  sender : Option Bool
  receiver : Option Bool
  deriving DecidableEq, Repr

def receiptStatus (now : Nat) (h : Option Handoff) : Status :=
  match h with
  | none => .HOLD
  | some v => worst [bindingStatus v.binding, timeStatus now v.observed v.expires,
      checked v.partiesMatch, required v.quantity, required v.condition,
      required v.sender, required v.receiver]

structure Observation where
  now : Nat
  targetMatches : Bool
  existingCargo : List (Option Nat × Nat)
  weights : List (Option Nat)
  pallets : List (Option Nat)
  mixing : List (Option Bool)
  deliveries : List Delivery
  stopOrder : List (Option Nat × Option Nat)
  termsMatch : Bool
  agreements : List (Option Bool)
  rolesMatch : Bool
  disclosureAllowed : Bool
  approvalShape : Bool
  approvals : List (Option Approval)
  evidence : Option Evidence
  scopeAllowed : Bool
  handoff : Option Handoff
  deriving DecidableEq, Repr

/-- 型の上では任意長でも、空の検査群が承認を生まないよう個数を検査する。 -/
def checks (o : Observation) : List Status :=
  [worst ([checked o.targetMatches, checked (decide (o.existingCargo.length = 4))] ++
      o.existingCargo.map (fun v => sameNat v.1 v.2)),
   worst ([checked (decide (o.weights.length = 3 ∧ o.pallets.length = 3 ∧ o.mixing.length = 3)),
      resourceStatus o.weights 4000, resourceStatus o.pallets 10] ++ o.mixing.map required),
   worst ([checked (decide (o.deliveries.length = 3 ∧ o.stopOrder.length = 2))] ++
      o.deliveries.map deliveryStatus ++ o.stopOrder.map (fun v => orderStatus v.1 v.2)),
   worst ([checked o.termsMatch, checked o.rolesMatch, checked (decide (o.agreements.length = 4))] ++
      o.agreements.map required),
   checked o.disclosureAllowed,
   worst ([checked o.approvalShape, checked (decide (o.approvals.length = 4))] ++
      o.approvals.map (approvalStatus o.now)),
   evidenceStatus o.now o.evidence,
   checked o.scopeAllowed]

def participation (o : Observation) : Status := worst (checks o)

def handoff (o : Observation) : Status :=
  worst [participation o, receiptStatus o.now o.handoff]

def ready (o : Observation) : Bool :=
  decide (participation o = .PASS ∧ handoff o = .PASS)

structure Economics where
  revenue : Option Nat
  costs : List (Option Nat)
  deriving DecidableEq, Repr

def sumSome : List (Option Nat) → Option Nat
  | [] => some 0
  | none :: _ => none
  | some n :: xs => match sumSome xs with
      | none => none
      | some total => some (n + total)

def contribution (e : Economics) : Option Int :=
  match e.revenue, sumSome e.costs with
  | some revenue, some costs => some (Int.ofNat revenue - Int.ofNat costs)
  | _, _ => none

def business (e : Economics) : BusinessStatus :=
  match contribution e with
  | none => .UNKNOWN
  | some net => if net < 0 then .NEGATIVE else if 0 < net then .POSITIVE else .BREAKEVEN

def assessment (o : Observation) (e : Economics) : Status × BusinessStatus :=
  (participation o, business e)

structure Gate where
  snapshotVerified : Bool
  permitMatches : Bool
  planMatches : Bool
  clockMonotone : Bool
  current : Observation
  nonce : String
  used : List String
  deriving DecidableEq, Repr

def canUse (g : Gate) : Bool :=
  g.snapshotVerified && g.permitMatches && g.planMatches && g.clockMonotone &&
  ready g.current && !(decide (g.nonce ∈ g.used))

def updatedUsed (g : Gate) : List String :=
  if canUse g then g.used ++ [g.nonce] else g.used

/-- 内容の再計算一致と現在の利用可否を分ける最小の記録照合モデル。 -/
def replayMatches (recorded recomputed : Status) : Bool := decide (recorded = recomputed)

-- Aggregation and non-vacuous checks.
theorem worst_reject_iff (xs : List Status) :
    worst xs = .REJECT ↔ Status.REJECT ∈ xs := by
  by_cases hr : Status.REJECT ∈ xs <;>
    by_cases hh : Status.HOLD ∈ xs <;> simp [worst, hr, hh]

theorem worst_hold_iff (xs : List Status) :
    worst xs = .HOLD ↔ Status.REJECT ∉ xs ∧ Status.HOLD ∈ xs := by
  by_cases hr : Status.REJECT ∈ xs <;>
    by_cases hh : Status.HOLD ∈ xs <;> simp [worst, hr, hh]

theorem worst_pass_iff_no_blockers (xs : List Status) :
    worst xs = .PASS ↔ Status.REJECT ∉ xs ∧ Status.HOLD ∉ xs := by
  by_cases hr : Status.REJECT ∈ xs <;>
    by_cases hh : Status.HOLD ∈ xs <;> simp [worst, hr, hh]

theorem worst_pass_iff (xs : List Status) :
    worst xs = .PASS ↔ ∀ s ∈ xs, s = Status.PASS := by
  constructor
  · intro h s hs
    have hn := (worst_pass_iff_no_blockers xs).mp h
    cases s with
    | PASS => rfl
    | HOLD => exact False.elim (hn.2 hs)
    | REJECT => exact False.elim (hn.1 hs)
  · intro h
    apply (worst_pass_iff_no_blockers xs).mpr
    constructor
    · intro hm
      have bad := h Status.REJECT hm
      cases bad
    · intro hm
      have bad := h Status.HOLD hm
      cases bad

theorem blocker_prevents_pass (xs : List Status) (s : Status)
    (hm : s ∈ xs) (hb : s ≠ .PASS) : worst xs ≠ .PASS := by
  intro h
  exact hb ((worst_pass_iff xs).mp h s hm)

theorem append_cannot_remove_reject (xs ys : List Status)
    (h : worst xs = .REJECT) : worst (xs ++ ys) = .REJECT := by
  apply (worst_reject_iff (xs ++ ys)).mpr
  exact List.mem_append.mpr (Or.inl ((worst_reject_iff xs).mp h))

theorem checked_pass_iff (v : Bool) : checked v = .PASS ↔ v = true := by
  cases v <;> decide

theorem required_unknown : required none = .HOLD := rfl

theorem required_denial : required (some false) = .REJECT := rfl

theorem required_confirmation : required (some true) = .PASS := rfl

theorem required_pass_iff (v : Option Bool) :
    required v = .PASS ↔ v = some true := by
  cases v with
  | none => decide
  | some b => cases b <;> decide

theorem missing_quantity_not_pass (xs : List (Option Nat)) (limit : Nat)
    (h : none ∈ xs) : resourceStatus xs limit ≠ .PASS := by
  by_cases hc : knownSum xs ≤ limit <;> simp [resourceStatus, hc, h]

theorem resource_pass_respects_limit (xs : List (Option Nat)) (limit : Nat)
    (h : resourceStatus xs limit = .PASS) : knownSum xs ≤ limit := by
  by_cases hc : knownSum xs ≤ limit
  · exact hc
  · simp [resourceStatus, hc] at h

theorem known_overload_rejected (xs : List (Option Nat)) (limit : Nat)
    (h : ¬ knownSum xs ≤ limit) : resourceStatus xs limit = .REJECT := by
  simp [resourceStatus, h]

theorem time_pass_iff (now begins ends : Nat) :
    timeStatus now begins ends = .PASS ↔ begins ≤ now ∧ now < ends := by
  by_cases h : begins ≤ now ∧ now < ends <;> simp [timeStatus, h]

theorem expiry_is_exclusive (now begins : Nat) :
    timeStatus now begins now = .HOLD := by
  simp [timeStatus]

theorem missing_approval_is_hold (now : Nat) :
    approvalStatus now none = .HOLD := rfl

theorem approval_pass_requires_current_time (now : Nat) (a : Approval)
    (h : approvalStatus now (some a) = .PASS) :
    a.issued ≤ now ∧ now < a.expires := by
  have ht : timeStatus now a.issued a.expires = .PASS :=
    (worst_pass_iff _).mp h _ (by simp)
  exact (time_pass_iff now a.issued a.expires).mp ht

theorem revoked_approval_not_pass (now : Nat) (a : Approval)
    (h : a.active = false) : approvalStatus now (some a) ≠ .PASS := by
  apply blocker_prevents_pass _ Status.HOLD
  · simp [approvalStatus, h]
  · decide

theorem missing_evidence_is_hold (now : Nat) :
    evidenceStatus now none = .HOLD := rfl

theorem missing_receipt_is_hold (now : Nat) :
    receiptStatus now none = .HOLD := rfl

theorem receipt_pass_requires_receiver (now : Nat) (h : Handoff)
    (hp : receiptStatus now (some h) = .PASS) : h.receiver = some true := by
  have hr : required h.receiver = .PASS :=
    (worst_pass_iff _).mp hp _ (by simp)
  exact (required_pass_iff h.receiver).mp hr

theorem eight_participation_checks (o : Observation) : (checks o).length = 8 := rfl

theorem participation_pass_requires_every_check (o : Observation)
    (h : participation o = .PASS) : ∀ s ∈ checks o, s = Status.PASS := by
  exact (worst_pass_iff (checks o)).mp h

theorem failed_check_blocks_participation (o : Observation) (s : Status)
    (hm : s ∈ checks o) (hs : s ≠ .PASS) : participation o ≠ .PASS := by
  exact blocker_prevents_pass (checks o) s hm hs

theorem handoff_pass_requires_participation (o : Observation)
    (h : handoff o = .PASS) : participation o = .PASS := by
  exact (worst_pass_iff _).mp h _ (by simp)

theorem handoff_pass_requires_receipt (o : Observation)
    (h : handoff o = .PASS) : receiptStatus o.now o.handoff = .PASS := by
  exact (worst_pass_iff _).mp h _ (by simp)

theorem missing_receipt_blocks_handoff (o : Observation)
    (h : o.handoff = none) : handoff o ≠ .PASS := by
  intro hp
  have hr := handoff_pass_requires_receipt o hp
  simp [h, receiptStatus] at hr

theorem economics_cannot_change_participation (o : Observation) (a b : Economics) :
    (assessment o a).1 = (assessment o b).1 := rfl

theorem missing_revenue_is_unknown (e : Economics) (h : e.revenue = none) :
    business e = .UNKNOWN := by
  simp [business, contribution, h]

theorem loss_is_not_profit (e : Economics) (n : Int)
    (h : contribution e = some n) (hn : n < 0) : business e = .NEGATIVE := by
  simp [business, h, hn]

theorem zero_margin_is_breakeven (e : Economics)
    (h : contribution e = some 0) : business e = .BREAKEVEN := by
  simp [business, h]

theorem invalid_snapshot_blocks_gate (g : Gate) (h : g.snapshotVerified = false) :
    canUse g = false := by
  simp [canUse, h]

theorem changed_plan_blocks_gate (g : Gate) (h : g.planMatches = false) :
    canUse g = false := by
  simp [canUse, h]

theorem clock_rollback_blocks_gate (g : Gate) (h : g.clockMonotone = false) :
    canUse g = false := by
  simp [canUse, h]

theorem not_ready_blocks_gate (g : Gate) (h : ready g.current = false) :
    canUse g = false := by
  simp [canUse, h]

theorem used_nonce_blocks_gate (g : Gate) (h : g.nonce ∈ g.used) :
    canUse g = false := by
  simp [canUse, h]

theorem blocked_gate_preserves_state (g : Gate) (h : canUse g = false) :
    updatedUsed g = g.used := by
  simp [updatedUsed, h]

theorem successful_gate_records_nonce (g : Gate) (h : canUse g = true) :
    g.nonce ∈ updatedUsed g := by
  simp [updatedUsed, h]

theorem repeated_gate_is_blocked (g : Gate) (h : canUse g = true) :
    canUse { g with used := updatedUsed g } = false := by
  exact used_nonce_blocks_gate { g with used := updatedUsed g }
    (successful_gate_records_nonce g h)

theorem hold_record_can_verify : replayMatches .HOLD .HOLD = true := by decide

theorem reject_record_can_verify : replayMatches .REJECT .REJECT = true := by decide

theorem verified_hold_is_not_pass :
    replayMatches .HOLD .HOLD = true ∧ Status.HOLD ≠ Status.PASS := by decide

theorem changed_record_does_not_verify : replayMatches .PASS .REJECT = false := by decide

end JointDeliveryAssurance

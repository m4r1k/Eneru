#!/bin/bash

# UPS Monitor Script for NUT Server
# Monitors UPS status and triggers safe shutdown on low battery

# Configuration
UPS_NAME="UPS@192.168.178.11"
CHECK_INTERVAL=1  # seconds between checks (1 second for aggressive monitoring)
LOW_BATTERY_THRESHOLD=20  # percentage - immediate shutdown trigger
BATTERY_DEPLETION_WINDOW=60  # seconds to calculate depletion rate
CRITICAL_DEPLETION_RATE=10.0  # %/minute - trigger early warning
CRITICAL_RUNTIME_THRESHOLD=600  # seconds - shutdown if runtime < 10 minutes
LOG_FILE="/var/log/ups-monitor.log"
STATE_FILE="/var/run/ups-monitor.state"
SHUTDOWN_SCHEDULED_FILE="/var/run/ups-shutdown-scheduled"
BATTERY_HISTORY_FILE="/var/run/ups-battery-history"

# Dry-run mode: set to "true" to test without actually shutting down
DRY_RUN_MODE="true"

# Extended time on battery
EXTENDED_TIME="$(echo "15*60"|bc 2>/dev/null || echo "900")"
EXTENDED_TIME_ON_BATTERY_SHUTDOWN="false"

# Remote NAS configuration
REMOTE_NAS_USER="nas-admin"
REMOTE_NAS_HOST="192.168.178.229"
REMOTE_NAS_PASSWORD="<Super Secure Synology Password>"

# Ensure log file exists
touch "$LOG_FILE"

# Initialize battery history file
> "$BATTERY_HISTORY_FILE"

# Function to log messages
log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z') - $1" | tee -a "$LOG_FILE"
}

# Check for required commands
if ! command -v bc &> /dev/null; then
    log_message "❌ FATAL ERROR: bc command not found. Please install bc package."
    exit 1
fi

if ! command -v upsc &> /dev/null; then
    log_message "❌ FATAL ERROR: upsc command not found. Please install nut-client package."
    exit 1
fi

# Function to get UPS variable using upsc
get_ups_var() {
    local var_name="$1"
    upsc "$UPS_NAME" "$var_name" 2>/dev/null
}

# Function to calculate battery depletion rate
calculate_depletion_rate() {
    if [ ! -f "$BATTERY_HISTORY_FILE" ]; then
        echo "0"
        return
    fi
    
    local current_time=$(date +%s)
    local current_battery="$1"
    
    # Keep only last 60 seconds of history with proper validation
    local cutoff_time=$((current_time - BATTERY_DEPLETION_WINDOW))
    awk -F: -v cutoff="$cutoff_time" '$1 >= cutoff && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9.]+$/' \
        "$BATTERY_HISTORY_FILE" > "${BATTERY_HISTORY_FILE}.tmp" 2>/dev/null
    mv "${BATTERY_HISTORY_FILE}.tmp" "$BATTERY_HISTORY_FILE" 2>/dev/null
    
    # Add current reading
    echo "${current_time}:${current_battery}" >> "$BATTERY_HISTORY_FILE"
    
    # Calculate depletion rate if we have enough history (15 samples = ~15 seconds)
    local line_count=$(wc -l < "$BATTERY_HISTORY_FILE" 2>/dev/null || echo "0")
    if [ "$line_count" -ge 15 ]; then
        local oldest=$(head -1 "$BATTERY_HISTORY_FILE")
        local oldest_time=$(echo "$oldest" | cut -d: -f1)
        local oldest_battery=$(echo "$oldest" | cut -d: -f2)
        
        # Validate data
        if [ -z "$oldest_time" ] || [ -z "$oldest_battery" ]; then
            echo "0"
            return
        fi

        local time_diff=$((current_time - oldest_time))
        if [ "$time_diff" -gt 0 ]; then
            local battery_diff=$(echo "$oldest_battery - $current_battery" | bc 2>/dev/null)
            if [ -n "$battery_diff" ]; then
                # Convert to %/minute, handle negative values (battery charging)
                local rate=$(echo "scale=2; ($battery_diff / $time_diff) * 60" | bc 2>/dev/null)
                if [ -n "$rate" ]; then
                    echo "$rate"
                    return
                fi
            fi
        fi
    fi
    
    echo "0"
}

# Function to execute controlled shutdown sequence
execute_shutdown_sequence() {
    log_message "🚨 ========== INITIATING EMERGENCY SHUTDOWN SEQUENCE =========="
    
    if [ "$DRY_RUN_MODE" = "true" ]; then
        log_message "🧪 *** DRY-RUN MODE: No actual shutdown will occur ***"
    fi
    
    # Send final warning to all users
    if [ "$DRY_RUN_MODE" = "true" ]; then
        wall "[DRY-RUN] 🚨 CRITICAL: Would execute emergency UPS shutdown sequence NOW!" 2>/dev/null
    else
        wall "🚨 CRITICAL: Executing emergency UPS shutdown sequence NOW!" 2>/dev/null
    fi
    
    # Shutdown all libvirt VMs
    log_message "🖥️  Shutting down all libvirt virtual machines..."
    if command -v virsh &> /dev/null; then
        # Get list of running VMs
        RUNNING_VMS=$(virsh list --name --state-running 2>/dev/null)
        if [ -n "$RUNNING_VMS" ]; then
            for vm in $RUNNING_VMS; do
                if [ -n "$vm" ]; then
                    log_message "  ⏹️  Shutting down VM: $vm"
                    if [ "$DRY_RUN_MODE" = "true" ]; then
                        log_message "  🧪 [DRY-RUN] Would shutdown VM: $vm"
                    else
                        virsh shutdown "$vm" 2>&1 | tee -a "$LOG_FILE"
                    fi
                fi
            done
            
            # Wait a few seconds for graceful shutdown
            if [ "$DRY_RUN_MODE" != "true" ]; then
                log_message "  ⏳ Waiting 10 seconds for VMs to shutdown gracefully..."
                sleep 10
                
                # Force destroy any remaining VMs
                STILL_RUNNING=$(virsh list --name --state-running 2>/dev/null)
                if [ -n "$STILL_RUNNING" ]; then
                    for vm in $STILL_RUNNING; do
                        if [ -n "$vm" ]; then
                            log_message "  ⚡ Force destroying VM: $vm"
                            virsh destroy "$vm" 2>&1 | tee -a "$LOG_FILE"
                        fi
                    done
                fi
            fi
            log_message "  ✅ All VMs shutdown complete"
        else
            log_message "  ℹ️  No running VMs found"
        fi
    else
        log_message "  ⚠️  virsh command not found, skipping VM shutdown"
    fi
    
    # Stop all Docker containers
    log_message "🐋 Stopping all Docker containers..."
    if command -v docker &> /dev/null; then
        RUNNING_CONTAINERS=$(docker ps -q 2>/dev/null)
        if [ -n "$RUNNING_CONTAINERS" ]; then
            if [ "$DRY_RUN_MODE" = "true" ]; then
                log_message "  🧪 [DRY-RUN] Would stop Docker containers: $(docker ps --format '{{.Names}}' | tr '\n' ' ')"
            else
                docker stop $(docker ps -q) 2>&1 | tee -a "$LOG_FILE"
                log_message "  ✅ Docker containers stopped"
            fi
        else
            log_message "  ℹ️  No running Docker containers found"
        fi
    else
        log_message "  ⚠️  Docker command not found, skipping container shutdown"
    fi
    
    # Sync all filesystems
    log_message "💾 Syncing all filesystems..."
    if [ "$DRY_RUN_MODE" = "true" ]; then
        log_message "  🧪 [DRY-RUN] Would sync filesystems"
    else
        sync
        sync
        sync
        log_message "  ✅ Filesystems synced"
    fi
    
    # Unmount specific mounts
    log_message "📤 Unmounting /mnt/media..."
    if [ "$DRY_RUN_MODE" = "true" ]; then
        log_message "  🧪 [DRY-RUN] Would unmount /mnt/media"
    else
        umount /mnt/media 2>/dev/null && log_message "  ✅ /mnt/media unmounted" || log_message "  ⚠️  Failed to unmount /mnt/media (may not be mounted)"
    fi
    
    log_message "📤 Unmounting /mnt/nas..."
    if [ "$DRY_RUN_MODE" = "true" ]; then
        log_message "  🧪 [DRY-RUN] Would unmount /mnt/nas"
    else
        umount /mnt/nas 2>/dev/null && log_message "  ✅ /mnt/nas unmounted" || log_message "  ⚠️  Failed to unmount /mnt/nas (may not be mounted)"
    fi
    
    # Shutdown remote NAS
    log_message "🌐 Initiating remote NAS shutdown at $REMOTE_NAS_HOST..."
    if [ "$DRY_RUN_MODE" = "true" ]; then
        log_message "  🧪 [DRY-RUN] Would send shutdown command to $REMOTE_NAS_USER@$REMOTE_NAS_HOST"
    else
        sshpass -p "$REMOTE_NAS_PASSWORD" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
            "${REMOTE_NAS_USER}@${REMOTE_NAS_HOST}" \
            "echo $REMOTE_NAS_PASSWORD | sudo -S shutdown -h now" 2>&1 | tee -a "$LOG_FILE"
        
        if [ $? -eq 0 ]; then
            log_message "  ✅ Remote NAS shutdown command sent successfully"
        else
            log_message "  ❌ WARNING: Failed to send shutdown command to remote NAS"
        fi
    fi
    
    # Final sync
    log_message "💾 Final filesystem sync..."
    if [ "$DRY_RUN_MODE" = "true" ]; then
        log_message "  🧪 [DRY-RUN] Would perform final sync"
    else
        sync
        log_message "  ✅ Final sync complete"
    fi
    
    # Shutdown local server
    log_message "🔌 Shutting down local server NOW"
    log_message "✅ ========== SHUTDOWN SEQUENCE COMPLETE =========="
    
    if [ "$DRY_RUN_MODE" = "true" ]; then
        log_message "🧪 [DRY-RUN] Would execute: shutdown -h now"
        log_message "🧪 [DRY-RUN] Shutdown sequence completed successfully (no actual shutdown)"
        # In dry-run mode, remove the scheduled file so we can test again
        rm -f "$SHUTDOWN_SCHEDULED_FILE"
    else
        # Immediate shutdown
        shutdown -h now "UPS battery critical - emergency shutdown"
    fi
}

# Function to cancel scheduled shutdown
cancel_shutdown() {
    if [ -f "$SHUTDOWN_SCHEDULED_FILE" ]; then
        log_message "✋ CANCELLING scheduled shutdown - power restored"
        if [ "$DRY_RUN_MODE" = "true" ]; then
            log_message "🧪 [DRY-RUN] Would cancel shutdown"
        else
            shutdown -c 2>/dev/null
        fi
        rm -f "$SHUTDOWN_SCHEDULED_FILE"
        rm -f "$BATTERY_HISTORY_FILE"
        wall "✅ UPS shutdown cancelled - Power has been restored" 2>/dev/null
    fi
}

# Function to trigger immediate shutdown
trigger_immediate_shutdown() {
    if [ ! -f "$SHUTDOWN_SCHEDULED_FILE" ]; then
        touch "$SHUTDOWN_SCHEDULED_FILE"
        log_message "🚨 CRITICAL: Triggering immediate shutdown"
        wall "🚨 CRITICAL: UPS battery critical! Immediate shutdown initiated!" 2>/dev/null
        execute_shutdown_sequence
    fi
}

# Function to log power events
log_power_event() {
    local event="$1"
    local details="$2"
    log_message "⚡ POWER EVENT: $event - $details"
    logger -t ups-monitor -p daemon.warning "⚡ POWER EVENT: $event - $details"
}

# Initialize state tracking
PREVIOUS_STATUS=""
PREVIOUS_INPUT_VOLTAGE=""
ON_BATTERY_START_TIME=0
EXTENDED_TIME_LOGGED=false

log_message "🚀 UPS Monitor starting - monitoring $UPS_NAME using upsc"

if [ "$DRY_RUN_MODE" = "true" ]; then
    log_message "🧪 *** RUNNING IN DRY-RUN MODE - NO ACTUAL SHUTDOWN WILL OCCUR ***"
fi

# Main monitoring loop
while true; do
    # Query UPS using upsc
    UPS_STATUS=$(get_ups_var "ups.status")
    BATTERY_CHARGE=$(get_ups_var "battery.charge")
    BATTERY_RUNTIME=$(get_ups_var "battery.runtime")
    INPUT_VOLTAGE=$(get_ups_var "input.voltage")
    INPUT_FREQUENCY=$(get_ups_var "input.frequency")
    OUTPUT_VOLTAGE=$(get_ups_var "output.voltage")
    UPS_LOAD=$(get_ups_var "ups.load")
    
    # Check if we got valid data
    if [ -z "$UPS_STATUS" ]; then
        log_message "❌ ERROR: Cannot connect to UPS $UPS_NAME - check upsc configuration"
        sleep "$CHECK_INTERVAL"
        continue
    fi
    
    # Save current state (atomic write)
    cat > "${STATE_FILE}.tmp" <<EOF
STATUS=$UPS_STATUS
BATTERY=$BATTERY_CHARGE
RUNTIME=$BATTERY_RUNTIME
LOAD=$UPS_LOAD
INPUT_VOLTAGE=$INPUT_VOLTAGE
OUTPUT_VOLTAGE=$OUTPUT_VOLTAGE
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
EOF
    mv "${STATE_FILE}.tmp" "$STATE_FILE"
    
    # Detect status changes
    if [ "$UPS_STATUS" != "$PREVIOUS_STATUS" ] && [ -n "$PREVIOUS_STATUS" ]; then
        log_message "🔄 Status changed: $PREVIOUS_STATUS -> $UPS_STATUS (Battery: $BATTERY_CHARGE%, Runtime: ${BATTERY_RUNTIME}s, Load: $UPS_LOAD%)"
    fi
    
    # Check for power events
    if [[ "$UPS_STATUS" == *"OB"* ]]; then
        # On Battery
        if [[ "$PREVIOUS_STATUS" != *"OB"* ]]; then
            ON_BATTERY_START_TIME=$(date +%s)
            EXTENDED_TIME_LOGGED=false
            # Initialize fresh battery history for this outage
            > "$BATTERY_HISTORY_FILE"
            log_power_event "ON_BATTERY" "Battery: $BATTERY_CHARGE%, Runtime: $BATTERY_RUNTIME seconds, Load: $UPS_LOAD%"
            wall "⚠️  WARNING: Power failure detected! System running on UPS battery ($BATTERY_CHARGE% remaining, ${BATTERY_RUNTIME}s runtime)" 2>/dev/null
        fi
        
        # Calculate battery depletion rate
        DEPLETION_RATE=$(calculate_depletion_rate "$BATTERY_CHARGE")
        
        # Multiple shutdown triggers for safety
        
        # 1. Critical battery level
        if [ -n "$BATTERY_CHARGE" ] && [ "${BATTERY_CHARGE%.*}" -lt "$LOW_BATTERY_THRESHOLD" ]; then
            log_message "🔋 CRITICAL: Battery at ${BATTERY_CHARGE}% (threshold: ${LOW_BATTERY_THRESHOLD}%)"
            trigger_immediate_shutdown
        fi
        
        # 2. Critical runtime remaining
        if [ -n "$BATTERY_RUNTIME" ] && [ "${BATTERY_RUNTIME%.*}" -lt "$CRITICAL_RUNTIME_THRESHOLD" ]; then
            log_message "⏱️  CRITICAL: Only ${BATTERY_RUNTIME}s runtime remaining (threshold: ${CRITICAL_RUNTIME_THRESHOLD}s)"
            trigger_immediate_shutdown
        fi
        
        # 3. Dangerous depletion rate (only check if positive depletion)
        if [ -n "$DEPLETION_RATE" ] && [ "$DEPLETION_RATE" != "0" ]; then
            # Check if depletion rate is positive and above threshold
            if (( $(echo "$DEPLETION_RATE > 0 && $DEPLETION_RATE > $CRITICAL_DEPLETION_RATE" | bc -l) )); then
                log_message "📉 CRITICAL: Battery depleting at ${DEPLETION_RATE}%/min (threshold: ${CRITICAL_DEPLETION_RATE}%/min)"
                trigger_immediate_shutdown
            fi
        fi
        
        # 4. Extended time on battery (safety net)
        CURRENT_TIME=$(date +%s)
        TIME_ON_BATTERY=$((CURRENT_TIME - ON_BATTERY_START_TIME))
        if [ "$TIME_ON_BATTERY" -gt "$EXTENDED_TIME" ]; then
            if [ "$EXTENDED_TIME_ON_BATTERY_SHUTDOWN" = "true" ]; then
                log_message "⏳ CRITICAL: System on battery for ${TIME_ON_BATTERY}s (threshold: ${EXTENDED_TIME}s) - initiating shutdown"
                trigger_immediate_shutdown
            elif [ "$EXTENDED_TIME_LOGGED" = "false" ]; then
                log_message "⏳ INFO: System on battery for ${TIME_ON_BATTERY}s exceeded threshold (${EXTENDED_TIME}s) - extended time shutdown disabled"
                EXTENDED_TIME_LOGGED=true
            fi
        fi
        
        # Log current status every 5 seconds while on battery
        if [ $(($(date +%s) % 5)) -eq 0 ]; then
            log_message "🔋 On battery: ${BATTERY_CHARGE}% (${BATTERY_RUNTIME}s), Load: ${UPS_LOAD}%, Depletion: ${DEPLETION_RATE}%/min, Time on battery: ${TIME_ON_BATTERY}s"
        fi
        
    elif [[ "$UPS_STATUS" == *"OL"* ]]; then
        # On Line (power restored)
        if [[ "$PREVIOUS_STATUS" == *"OB"* ]]; then
            TIME_ON_BATTERY=$(($(date +%s) - ON_BATTERY_START_TIME))
            log_power_event "POWER_RESTORED" "Battery: $BATTERY_CHARGE%, Input: ${INPUT_VOLTAGE}V, Was on battery for ${TIME_ON_BATTERY}s"
            wall "✅ Power has been restored. UPS back on line power. Battery at ${BATTERY_CHARGE}%." 2>/dev/null
            ON_BATTERY_START_TIME=0
            EXTENDED_TIME_LOGGED=false
        fi
        
        # Cancel any pending shutdown
        cancel_shutdown
        
        # Clear battery history when back on line
        rm -f "$BATTERY_HISTORY_FILE"
    fi
    
    # Check for brownout (significant voltage drop while still on line)
    if [ -n "$INPUT_VOLTAGE" ] && [ -n "$PREVIOUS_INPUT_VOLTAGE" ]; then
        VOLTAGE_DIFF=$(echo "$INPUT_VOLTAGE - $PREVIOUS_INPUT_VOLTAGE" | bc 2>/dev/null)
        if [ -n "$VOLTAGE_DIFF" ]; then
            if (( $(echo "$VOLTAGE_DIFF < -10" | bc -l 2>/dev/null) )); then
                log_power_event "BROWNOUT" "Voltage dropped from ${PREVIOUS_INPUT_VOLTAGE}V to ${INPUT_VOLTAGE}V"
            elif (( $(echo "$VOLTAGE_DIFF > 10" | bc -l 2>/dev/null) )); then
                log_power_event "VOLTAGE_SURGE" "Voltage increased from ${PREVIOUS_INPUT_VOLTAGE}V to ${INPUT_VOLTAGE}V"
            fi
        fi
    fi
    
    # Check for bypass mode
    if [[ "$UPS_STATUS" == *"BYPASS"* ]]; then
        log_power_event "BYPASS_MODE" "UPS in bypass mode - no protection active!"
    fi
    
    # Check for overload
    if [[ "$UPS_STATUS" == *"OVER"* ]]; then
        log_power_event "OVERLOAD" "UPS overload detected! Load: $UPS_LOAD%"
    fi
    
    # Update previous values
    PREVIOUS_STATUS="$UPS_STATUS"
    PREVIOUS_INPUT_VOLTAGE="$INPUT_VOLTAGE"
    
    # Wait before next check
    sleep "$CHECK_INTERVAL"
done

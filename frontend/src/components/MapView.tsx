import React from 'react'
import { MapContainer, TileLayer, Marker, Polyline } from 'react-leaflet'
import type { DayPlan } from '../types/plan'
import 'leaflet/dist/leaflet.css'

interface Props {
  dailyPlans: DayPlan[]
}

export default function MapView({ dailyPlans }: Props) {
  const points = dailyPlans.flatMap((d) =>
    d.activities.map((a) => [a.location.lat, a.location.lng] as [number, number])
  )

  if (points.length === 0) {
    return <div className="map-empty">行程确定后将在此显示路线地图</div>
  }

  const center = points[0]

  return (
    <MapContainer center={center} zoom={13} style={{ height: '300px', width: '100%' }}>
      <TileLayer url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
      {points.map((p, i) => (
        <Marker key={i} position={p} />
      ))}
      {points.length > 1 && <Polyline positions={points} color="blue" />}
    </MapContainer>
  )
}
